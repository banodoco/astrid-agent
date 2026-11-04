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


@pytest.fixture(autouse=True)
def configured(tmp_path,monkeypatch):
    monkeypatch.setenv(packs.ROOT_ENV,str(tmp_path/'user-data'))
    monkeypatch.setenv(packs.ARTIFACT_ENV,str(tmp_path/'build-cache'))
    verifier=tmp_path/'test-build-verifier.py'
    verifier.write_text("import json,sys,hashlib\nfrom pathlib import Path\np=Path(sys.argv[1]);x=json.loads(p.read_text());print(json.dumps({'schema':'astrid.element-build/v1','selectionDigest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest(),'elements':[{'projectId':e['projectId'],**{k:e['descriptor'][k] for k in ('packId','id','kind','revision')}} for e in x['elements']]}))")
    monkeypatch.setenv(packs.BUILD_ENV,json.dumps([sys.executable,str(verifier)]))
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


def test_missing_or_failed_compiler_never_advances_selection(monkeypatch):
    seed();first=packs.admit_project_pack('realm','project');selection=Path(first['manifestPath']);before=selection.read_bytes()
    monkeypatch.delenv(packs.BUILD_ENV)
    with pytest.raises(ValueError,match='not configured'):packs.admit_project_pack('realm','project')
    assert selection.read_bytes()==before
    monkeypatch.setenv(packs.BUILD_ENV,json.dumps([sys.executable,'-c','import sys;sys.exit(1)']))
    with pytest.raises(ValueError,match='build verification failed'):packs.admit_project_pack('realm','project')
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
    encoded=json.dumps(row['content']);assert str(Path.home()) not in encoded and 'component.tsx' not in encoded


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


def test_failed_init_build_does_not_create_authority_document(configured,monkeypatch):
    monkeypatch.delenv(packs.BUILD_ENV)
    with pytest.raises(ValueError,match='not configured'):packs.init_project_pack('realm','project')
    assert configured.rows=={}
    context=packs.inspect_project_pack('realm','project')
    assert context['documentVersion']==0
    assert (Path(context['packRoot'])/'pack.yaml').is_file()


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
