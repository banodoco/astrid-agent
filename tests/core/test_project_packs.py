import json
import re
import sys
import copy
import hashlib
import shutil
from contextlib import contextmanager
from types import SimpleNamespace
from astrid.sdk.contracts import DomainResult,ErrorObject
from pathlib import Path
import pytest
import yaml
import os
import subprocess

from astrid.core.element import project_packs as packs

# Wire grammar of Runtime's Idempotency-Key (runtime_protocol/service.py: IDEMPOTENCY_KEY_RE,
# enforced by validate_idempotency_key). Mirrored here so the fake client needs no runtime checkout.
_IDEMPOTENCY_KEY_RE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,255}')

def validate_idempotency_key(value):
    if not isinstance(value,str) or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:raise ValueError('Idempotency-Key must start with an alphanumeric character and contain at most 256 ASCII characters')
    return value

class FakeDocuments:
    def __init__(self):self.rows={};self.reads=[];self.writes=[];self.reject_update=False
    def _identity(self):return {'realm_id':'realm'}
    def show(self,document,*,project=None):
        self.reads.append((project,document))
        row=self.rows.get((project,document))
        return DomainResult.success(copy.deepcopy(row)) if row else DomainResult.failure(ErrorObject('not_found','missing',{}))
    def create(self,*,project,kind,document_id,content,idempotency_key):
        validate_idempotency_key(idempotency_key)
        if (project,document_id) in self.rows:return DomainResult.failure(ErrorObject('conflict','already exists',{}))
        row={'document_id':document_id,'kind':kind,'version':1,'content':copy.deepcopy(content)};self.rows[(project,document_id)]=row;self.writes.append((project,document_id));return DomainResult.success(copy.deepcopy(row))
    def update(self,document,*,project,content,expected_version,idempotency_key):
        validate_idempotency_key(idempotency_key)
        row=self.rows[(project,document)]
        if self.reject_update or row['version']!=expected_version:return DomainResult.failure(ErrorObject('conflict','CAS rejected',{}))
        row={**row,'version':row['version']+1,'content':copy.deepcopy(content)};self.rows[(project,document)]=row;self.writes.append((project,document));return DomainResult.success(copy.deepcopy(row))
    def list(self,*args,**kwargs):raise AssertionError('Must not scan all project documents')


class FakeMedia:
    def __init__(self):
        self.objects={}
        self.imports=[]
        self.reject=False
        self.metadata={}
    def import_bytes(self, *, project, data, filename, media_type, idempotency_key):
        validate_idempotency_key(idempotency_key)
        self.imports.append((project,data,idempotency_key))
        if self.reject:
            return DomainResult.failure(ErrorObject('conflict','ingest failed',{}))
        digest='sha256:'+hashlib.sha256(data).hexdigest()
        self.objects[(project,digest)]=data
        return DomainResult.success({'object_id':digest,'digest':digest,'size':len(data),
                                     'media_type':media_type,**self.metadata})
    def read_bytes(self,object_id):
        return next(data for (_,digest),data in self.objects.items() if digest==object_id)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def configured(tmp_path,monkeypatch):
    monkeypatch.setenv(packs.ROOT_ENV,str(tmp_path/'user-data'))
    monkeypatch.setenv(packs.ARTIFACT_ENV,str(tmp_path/'build-cache'))
    documents=FakeDocuments()
    documents.media=FakeMedia()
    client=SimpleNamespace(documents=documents,media=documents.media)
    @contextmanager
    def connected():yield client
    monkeypatch.setattr(packs,'_open_client',connected)
    return documents


def seed(project='project'):
    context=packs.init_project_pack('realm',project);root=Path(context['packRoot'])
    directory=root/'rendering/elements/effects/reveal';directory.mkdir(parents=True)
    (directory/'component.tsx').write_text("import React from 'react';\nimport {AbsoluteFill} from 'remotion';\nexport default function Reveal(){return <AbsoluteFill/>;}\n")
    (directory/'element.yaml').write_text(yaml.safe_dump({'schema_version':1,'id':'reveal','kind':'effect','pack_id':context['packId'],'short_description':'Test reveal.','defaults':{'seed':15},'schema':{'type':'object','properties':{'seed':{'type':'number'}}},'runtime':{'adapter':'remotion'}}))
    manifest=yaml.safe_load((root/'pack.yaml').read_text())
    manifest['rendering']={'effects/reveal':{'type':'element','path':'rendering/elements/effects/reveal/element.yaml','resources':[{'kind':'implementation','path':'rendering/elements/effects/reveal/component.tsx'}]}}
    (root/'pack.yaml').write_text(yaml.safe_dump(manifest))
    return root,directory,manifest


def add_second_element(root, directory, manifest):
    other = directory.parent / 'fade'
    shutil.copytree(directory, other)
    definition = yaml.safe_load((other / 'element.yaml').read_text())
    definition['id'] = 'fade'
    (other / 'element.yaml').write_text(yaml.safe_dump(definition))
    declaration = copy.deepcopy(manifest['rendering']['effects/reveal'])
    declaration['path'] = declaration['path'].replace('/reveal/', '/fade/')
    for resource in declaration['resources']:
        resource['path'] = resource['path'].replace('/reveal/', '/fade/')
    manifest['rendering']['effects/fade'] = declaration
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    return other


def test_readmission_of_b_only_keeps_a_unique_and_resolvable(configured):
    root, directory, manifest = seed()
    other = add_second_element(root, directory, manifest)
    first = packs.admit_project_pack('realm', 'project')
    old = {e['descriptor']['id']: e for e in first['elements']}
    component = other / 'component.tsx'
    component.write_text(component.read_text() + '\n// B only\n')
    second = packs.admit_project_pack('realm', 'project')
    current = {e['descriptor']['id']: e for e in second['elements']}
    assert current['reveal']['descriptor']['revision'] == old['reveal']['descriptor']['revision']
    assert current['fade']['descriptor']['revision'] != old['fade']['descriptor']['revision']
    row = configured.rows[('project', second['documentId'])]
    identities = [(a['id'], a['kind'], a['revision']) for a in row['content']['admissions']]
    # Main appended unchanged A under the new closure, making old A ambiguous.
    assert len(identities) == len(set(identities)) == 3
    for record in (old['reveal'], old['fade'], current['fade']):
        reference = {k: record['descriptor'][k] for k in ('packId', 'id', 'kind', 'revision')}
        assert packs.resolve_admitted_element('realm', 'project', reference)['descriptor'] == record['descriptor']

def test_reads_lazy_and_init_explicit():
    context=packs.inspect_project_pack('realm','project');assert context['status']=='not-created';assert not Path(context['packRoot']).exists()
    created=packs.init_project_pack('realm','project');assert (Path(created['packRoot'])/'pack.yaml').is_file()
    assert packs.init_project_pack('realm','project')['packId']==created['packId']

def test_immutable_snapshot_old_pin_retained():
    root,directory,_=seed();first=packs.admit_project_pack('realm','project');record=first['elements'][0]
    snapshot=Path(record['snapshot']['root'])/record['snapshot']['componentPath'];original=snapshot.read_bytes()
    (directory/'component.tsx').write_text((directory/'component.tsx').read_text()+'\n// changed')
    assert packs.inspect_project_pack('realm','project')['status']=='not-yet-admitted'
    second=packs.admit_project_pack('realm','project');assert second['elements'][0]['descriptor']['revision']!=record['descriptor']['revision']
    assert len(json.loads(Path(first['manifestPath']).read_text())['elements'])==2;assert snapshot.read_bytes()==original

def test_selection_retains_two_projects(configured):
    seed('first');first=packs.admit_project_pack('realm','first');seed('second');second=packs.admit_project_pack('realm','second')
    assert first['manifestPath']!=second['manifestPath']
    assert json.loads(Path(first['manifestPath']).read_text())['binding']['projectId']=='first'
    assert json.loads(Path(second['manifestPath']).read_text())['binding']['projectId']=='second'
    assert len(configured.rows)==2

def test_failed_admission_rolls_back():
    _,directory,_=seed();first=packs.admit_project_pack('realm','project');selection=Path(first['manifestPath']);before=selection.read_bytes()
    (directory/'component.tsx').write_text("import nope from 'arbitrary-library';export default nope;")
    with pytest.raises(ValueError,match='Unsupported dependency'):packs.admit_project_pack('realm','project')
    assert selection.read_bytes()==before

def test_symlink_and_undeclared_import_fail():
    _,directory,_=seed();(directory/'component.tsx').write_text("import helper from './helper';export default helper;");(directory/'helper.ts').write_text('export default ()=>null;')
    with pytest.raises(ValueError,match='Undeclared'):packs.admit_project_pack('realm','project')
    (directory/'helper.ts').unlink();(directory/'helper.ts').symlink_to('/etc/hosts')
    with pytest.raises(ValueError,match='symlink'):packs.admit_project_pack('realm','project')

def test_missing_source_preserves_artifact():
    root,_,_=seed();first=packs.admit_project_pack('realm','project');(root/'pack.yaml').unlink()
    context=packs.inspect_project_pack('realm','project');assert context['status']=='source-missing' and context['admittedRevisions'];assert Path(first['elements'][0]['snapshot']['root']).is_dir()

def test_root_not_checkout(tmp_path,monkeypatch):
    monkeypatch.setenv(packs.ROOT_ENV,str(tmp_path/'.otto'/'user-code'))
    with pytest.raises(ValueError,match='durable'):packs.inspect_project_pack('realm','project')

def test_exact_admitted_ref_verifies_snapshot_and_wrong_owner_fails():
    seed();record=packs.admit_project_pack('realm','project')['elements'][0]
    ref={k:record['descriptor'][k] for k in ('id','kind','packId','revision')}
    assert packs.resolve_admitted_element('realm','project',ref)['descriptor']==record['descriptor']
    with pytest.raises(ValueError,match='not admitted'):packs.resolve_admitted_element('realm','other',ref)
    with pytest.raises(ValueError,match='not admitted'):packs.resolve_admitted_element('realm','project',{**ref,'packId':'wrong'})
    path=Path(record['snapshot']['root'])/record['snapshot']['componentPath'];path.write_text('changed')
    with pytest.raises(ValueError,match='corrupt'):packs.resolve_admitted_element('realm','project',ref)


def test_full_revision_includes_schema_declared_helper_and_asset():
    root,directory,manifest=seed()
    (directory/'helper.ts').write_text('export const amount=1;')
    (directory/'image.png').write_bytes(b'first-asset')
    declaration=manifest['rendering']['effects/reveal']
    declaration['resources'] += [{'kind':'implementation','path':'rendering/elements/effects/reveal/helper.ts'},{'kind':'asset','path':'rendering/elements/effects/reveal/image.png'}]
    (root/'pack.yaml').write_text(yaml.safe_dump(manifest))
    descriptor=yaml.safe_load((directory/'element.yaml').read_text());descriptor['assets']={'image':'image.png'}
    (directory/'element.yaml').write_text(yaml.safe_dump(descriptor))
    first=packs.admit_project_pack('realm','project')['elements'][0]['descriptor']['revision']
    (directory/'helper.ts').write_text('export const amount=2;')
    second=packs.admit_project_pack('realm','project')['elements'][0]['descriptor']['revision'];assert second!=first
    (directory/'image.png').write_bytes(b'second-asset')
    third=packs.admit_project_pack('realm','project')['elements'][0]['descriptor']['revision'];assert third!=second
    descriptor['schema']['properties']['seed']['maximum']=100
    (directory/'element.yaml').write_text(yaml.safe_dump(descriptor))
    fourth=packs.admit_project_pack('realm','project')['elements'][0]['descriptor']['revision'];assert fourth!=third


def test_canonical_paths_and_commands_on_timeline_inspection():
    seed();record=packs.admit_project_pack('realm','project')['elements'][0]
    ref={k:record['descriptor'][k] for k in ('id','kind','packId','revision')}
    bundle={'base_parent':{'revision_id':'captured-head'},'parent':{'clips':[{'id':'same','at':0,'hold':2,'track':'V1','elementRef':ref,'params':{'seed':15}}]}}
    context=packs.timeline_element_authoring('realm','project','timeline',bundle)
    assert context['elements'][0]['authoring']['componentPath'].endswith('component.tsx')
    assert 'captured-head' in context['elements'][0]['updateCommand']
    assert context['elements'][0]['schema']['properties']['seed']
    assert context['projectPack']['admitCommand']
    bundle['parent']['clips'][0]['elementRef'].pop('packId')
    assert packs.timeline_element_authoring('realm','project','timeline',bundle)['elements'][0]['status']=='legacy-migration-required'


def test_missing_or_failed_compiler_never_advances_selection(monkeypatch, tmp_path):
    root, directory, _ = seed();first=packs.admit_project_pack('realm','project');selection=Path(first['manifestPath']);before=selection.read_bytes()
    bare=tmp_path/'bare-remotion';bare.mkdir();(bare/'package.json').write_text(json.dumps({'dependencies':{'esbuild':'0.27.7'}}))
    with monkeypatch.context() as remote:
        remote.setenv('ASTRID_REMOTION_PROJECT_DIR',str(bare))
        with pytest.raises(ValueError,match='Remotion esbuild.*is missing'):packs.admit_project_pack('realm','project')
    assert selection.read_bytes()==before
    (directory/'component.tsx').write_text('export default function Reveal( {\n')
    with pytest.raises(ValueError,match='component.tsx.*failed to compile'):packs.admit_project_pack('realm','project')
    assert selection.read_bytes()==before


def test_document_cas_conflict_preserves_source_and_snapshot_without_selection(configured):
    _,directory,_=seed();first=packs.admit_project_pack('realm','project');path=Path(first['manifestPath']);before=path.read_bytes()
    (directory/'component.tsx').write_text((directory/'component.tsx').read_text()+'\n// concurrent new source')
    configured.reject_update=True
    with pytest.raises(ValueError,match='CAS rejected'):packs.admit_project_pack('realm','project')
    assert path.read_bytes()==before
    assert 'concurrent new source' in (directory/'component.tsx').read_text()


def test_one_pack_change_does_not_scan_or_rewrite_1000_other_configs(configured):
    for i in range(1000):configured.rows[('project',f'other-{i}') ]={'kind':f'other{i}.config','version':1,'content':{'keep':i}}
    before=copy.deepcopy(configured.rows);seed();packs.admit_project_pack('realm','project')
    assert all(configured.rows[key]==value for key,value in before.items())
    assert len({identity for _,identity in configured.writes})==1
    assert len(configured.reads)<12
    row=next(row for (_,identity),row in configured.rows.items() if identity not in {f'other-{i}' for i in range(1000)})
    assert row['content']['schema']=='astrid.project-pack/v1'
    assert row['version']>1 and 'schema_version' not in row['content']
    # Portable admission facts only: no machine-local absolute path reaches the shared document.
    encoded=json.dumps(row['content']);assert str(Path.home()) not in encoded
    for admission in row['content']['admissions']:
        assert set(admission['authoring']) == {'packRoot','descriptorPath','componentPath','status'}
        assert not admission['authoring']['descriptorPath'].startswith('/') and not admission['authoring']['componentPath'].startswith('/')


def test_named_collection_uses_one_exact_stable_document(configured):
    context=packs.init_project_pack('realm','project',collection='special_effects')
    assert context['packId']=='special_effects'
    assert '--collection special_effects' in context['admitCommand']
    assert context['documentId']==packs.stable_document_id('project','astrid.project-pack','special_effects')
    assert len(configured.rows)==1


def test_missing_derived_index_reconstructs_from_selected_document_and_snapshot(configured):
    seed();record=packs.admit_project_pack('realm','project')['elements'][0]
    ref={k:record['descriptor'][k] for k in ('id','kind','packId','revision')}
    context=packs.inspect_project_pack('realm','project');Path(context['manifestPath']).unlink()
    before=len(configured.writes)
    assert packs.resolve_admitted_element('realm','project',ref)['descriptor']==record['descriptor']
    rebuilt=packs.materialize_project_pack('realm','project')
    assert Path(rebuilt['manifestPath']).is_file() and len(configured.writes)==before


def test_read_wrong_realm_does_not_create_or_write(configured):
    with pytest.raises(ValueError,match='authenticated'):packs.inspect_project_pack('otherrealm','project')
    assert configured.rows=={} and configured.writes==[]


def test_undeclared_descriptor_asset_cannot_advance_document(configured):
    root,directory,_=seed();before=copy.deepcopy(configured.rows)
    (directory/'image.png').write_bytes(b'asset-not-declared')
    descriptor=yaml.safe_load((directory/'element.yaml').read_text());descriptor['assets']={'image':'image.png'}
    (directory/'element.yaml').write_text(yaml.safe_dump(descriptor))
    with pytest.raises(ValueError,match='asset/support closure'):packs.admit_project_pack('realm','project')
    assert configured.rows==before


def _remotion_copy(tmp_path, *, bin_esbuild):
    """A Remotion project dir sharing the install's real esbuild native package, with ``bin/esbuild`` as given."""
    real = Path(os.environ['ASTRID_REMOTION_PROJECT_DIR'])
    project = tmp_path / 'remotion-copy'
    modules = project / 'node_modules'
    (modules / '@esbuild').mkdir(parents=True)
    shutil.copy(real / 'package.json', project / 'package.json')
    shutil.copytree(real / 'node_modules' / 'esbuild', modules / 'esbuild', ignore=shutil.ignore_patterns('bin'))
    for native in (real / 'node_modules' / '@esbuild').iterdir():
        (modules / '@esbuild' / native.name).symlink_to(native.resolve())
    if bin_esbuild is not None:
        (modules / 'esbuild' / 'bin').mkdir()
        (modules / 'esbuild' / 'bin' / 'esbuild').write_text(bin_esbuild)
    return project


def test_admission_compiles_when_bin_esbuild_is_a_js_shim(monkeypatch, tmp_path):
    shim = '#!/usr/bin/env node\nthrow new Error("shim is not a native executable");\n'
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(_remotion_copy(tmp_path, bin_esbuild=shim)))
    seed()
    result = packs.admit_project_pack('realm', 'project')
    compiled = result['elements'][0]['compiled']
    assert compiled['path'].startswith('compiled/')
    assert b'AbsoluteFill' in (Path(result['elements'][0]['snapshot']['root']) / compiled['path']).read_bytes()


def test_compiler_discovery_needs_no_bin_esbuild_and_forwards_windows_env(monkeypatch, tmp_path):
    # The Windows layout has no bin/esbuild and a native package of its own; discovery goes through the JS API only.
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(_remotion_copy(tmp_path, bin_esbuild=None)))
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setenv('SYSTEMROOT', 'C:\\Windows')
    node, api = packs._renderer_compiler()
    assert api.name == 'main.js' and api.parent.name == 'lib'
    assert packs._node_env() == {'PATH': '', 'LANG': 'C.UTF-8', 'SYSTEMROOT': 'C:\\Windows'}


def test_missing_esbuild_api_and_wrong_version_have_plain_fixes(monkeypatch, tmp_path):
    project = _remotion_copy(tmp_path, bin_esbuild=None)
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(project))
    package = json.loads((project / 'package.json').read_text())
    package['dependencies']['esbuild'] = '0.0.1'
    (project / 'package.json').write_text(json.dumps(package))
    with pytest.raises(ValueError, match=r'reports 0\.27\.7, but .* pins 0\.0\.1.*Repair the install'):packs._renderer_compiler()
    (project / 'node_modules' / 'esbuild' / 'lib' / 'main.js').unlink()
    with pytest.raises(ValueError, match=r'main\.js is missing.*Repair the install'):packs._renderer_compiler()


def _make_legacy(configured, first):
    """Rewrite an admission to the pre-compiled-module shape: source-only closure digest, no `compiled`, stale index."""
    key = ('project', first['documentId'])
    row = configured.rows[key]
    admission = row['content']['admissions'][0]
    record = first['elements'][0]
    source = [f for f in record['snapshot']['files'] if not f['path'].startswith('compiled/')]
    admission['snapshot_digest'] = packs._closure_digest(source)
    del admission['compiled']
    legacy = copy.deepcopy(record)
    legacy['snapshot']['files'] = source
    del legacy['compiled']
    packs._atomic_json(Path(first['manifestPath']), packs._index('realm', 'project', first['documentId'], row['version'], row['content'], [legacy]))
    return admission


def test_readmitting_unchanged_legacy_admission_upgrades_it_with_a_compiled_module(configured):
    seed()
    first = packs.admit_project_pack('realm', 'project')
    legacy = _make_legacy(configured, first)
    old_digest = legacy['snapshot_digest']
    with pytest.raises(ValueError, match='predates compiled modules'):
        packs.materialize_project_pack('realm', 'project')
    again = packs.admit_project_pack('realm', 'project')
    row = configured.rows[('project', first['documentId'])]
    admissions = row['content']['admissions']
    assert len(admissions) == 1
    upgraded = admissions[0]
    assert upgraded['revision'] == first['elements'][0]['descriptor']['revision']
    assert upgraded['snapshot_digest'] not in (old_digest, None) and upgraded['compiled']['path'].startswith('compiled/')
    assert upgraded['compiled'] == again['elements'][0]['compiled']
    snapshot = Path(again['elements'][0]['snapshot']['root'])
    assert hashlib.sha256((snapshot / upgraded['compiled']['path']).read_bytes()).hexdigest() == upgraded['compiled']['sha256']
    # The repair no longer loops: the index rebuilds from the document and the element resolves with its compiled file.
    selected = json.loads(Path(again['manifestPath']).read_text())['elements']
    assert [e['compiled'] for e in selected] == [upgraded['compiled']]
    Path(again['manifestPath']).unlink()
    rebuilt = packs.materialize_project_pack('realm', 'project')
    assert rebuilt['elements'][0]['compiled'] == upgraded['compiled']
    ref = {k: again['elements'][0]['descriptor'][k] for k in ('id', 'kind', 'packId', 'revision')}
    assert packs.resolve_admitted_element('realm', 'project', ref)['compiled'] == upgraded['compiled']
    # A second admit is a no-op on the document.
    version = row['version']
    packs.admit_project_pack('realm', 'project')
    assert configured.rows[('project', first['documentId'])]['version'] == version
