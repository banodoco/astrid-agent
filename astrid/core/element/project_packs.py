"""Project draft packs: ordinary author files and immutable admitted build inputs.

This is local source selection/configuration, never a second project/timeline store.
Reads are observational; only explicit init/admit creates files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack.canonical import validate_canonical_pack
from .catalog import _element_descriptor
from .schema import load_element_definition
from astrid.sdk.documents import stable_document_id

SCHEMA = "astrid.element-admissions/v1"
ROOT_ENV = "ASTRID_PROJECT_PACKS_ROOT"
ARTIFACT_ENV = "ASTRID_ELEMENT_ARTIFACT_ROOT"
DOCUMENT_KIND = "astrid.project-pack"
DOCUMENT_SCHEMA = "astrid.project-pack/v1"
SOURCE_BINDING = "project-packs"
BUILD_ENV = "ASTRID_ELEMENT_BUILD_COMMAND"
_ALLOWED_IMPORTS = {"react", "react/jsx-runtime", "remotion"}
_SOURCE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".css"}


def _identity(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("realm/project must be a canonical resource identity")
    return value


def authoring_root() -> Path:
    root = Path(os.environ.get(ROOT_ENV) or Path(os.environ.get("XDG_DATA_HOME",Path.home()/".local/share"))/"astrid/project-packs").expanduser().absolute()
    if any(p in {".otto", "site-packages"} for p in root.parts) or root.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("Project authoring root must be durable user data outside checkouts, .otto and site-packages")
    return root


def _json_digest(value: Any) -> str:
    return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def artifact_root() -> Path:
    return Path(os.environ.get(ARTIFACT_ENV) or Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))/'astrid/element-builds').expanduser().absolute()


def _open_client():
    from astrid.sdk import AstridClient
    return AstridClient.open_from_launcher(start_pack_host=False)


def _read_document(client: Any, realm: str, project: str, collection: str='default'):
    identity=client.documents._identity()
    if identity.get('realm_id')!=realm:raise ValueError('Project pack realm differs from authenticated Runtime')
    document_id=stable_document_id(project,DOCUMENT_KIND,collection)
    result=client.documents.show(document_id,project=project)
    if not result.ok:
        if result.error and result.error.code in {'not_found','document_not_found'}:return document_id,None
        raise ValueError(f'Project pack document read failed: {result.error}')
    row=result.data
    if not isinstance(row,dict) or row.get('kind')!=DOCUMENT_KIND or not isinstance(row.get('version'),int):raise ValueError('Malformed project pack document')
    content=row.get('content')
    if not isinstance(content,dict) or content.get('schema')!=DOCUMENT_SCHEMA or content.get('source_binding_key')!=SOURCE_BINDING:raise ValueError('Unsupported project pack payload schema/source binding')
    if not re.fullmatch(r'[a-z][a-z0-9_]*',str(content.get('pack_id',''))):raise ValueError('Invalid project pack owner')
    if set(content)!={'schema','pack_id','source_binding_key','relative_manifest','admissions'} or not isinstance(content['admissions'],list):raise ValueError('Invalid project pack payload fields')
    for admission in content['admissions']:
        if not isinstance(admission,dict) or set(admission)!={'id','kind','revision','snapshot_digest'} or admission['kind'] not in {'effect','animation','transition'} or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(admission['revision'])) or not re.fullmatch(r'[0-9a-f]{64}',str(admission['snapshot_digest'])):raise ValueError('Invalid selected pack admission')

    return document_id,row


def _content(realm: str, project: str, collection: str='default') -> dict[str,Any]:
    pack_id='project_'+hashlib.sha256(f'{realm}:{project}:{collection}'.encode()).hexdigest()[:20] if collection=='default' else collection
    if not re.fullmatch(r'[a-z][a-z0-9_]*',pack_id):raise ValueError('Invalid pack collection owner')
    return {'schema':DOCUMENT_SCHEMA,'pack_id':pack_id,'source_binding_key':SOURCE_BINDING,
            'relative_manifest':f'{realm}/{project}/packs/{pack_id}/pack.yaml','admissions':[]}


def _location(realm: str, project: str, content: dict[str,Any], document_id: str, version: int) -> dict[str,Any]:
    _identity(realm);_identity(project)
    relative=Path(content['relative_manifest'])
    if relative.is_absolute() or '..' in relative.parts or relative.name!='pack.yaml':raise ValueError('Invalid portable pack manifest path')
    root=authoring_root();manifest=root/relative
    if not manifest.resolve().is_relative_to(root.resolve()):raise ValueError('Pack binding escapes configured native root')
    pack_id=content['pack_id'];index=artifact_root()/realm/project/pack_id/'selection.json'
    prefix='python3 -m astrid.core.element.cli project-pack';scope=f' --realm {shlex.quote(realm)} --project {shlex.quote(project)}'
    if document_id!=stable_document_id(project,DOCUMENT_KIND,'default'):scope+=f' --collection {shlex.quote(pack_id)}'
    return {'projectId':project,'packId':pack_id,'packRoot':str(manifest.parent),'documentId':document_id,'documentVersion':version,
            'manifestPath':str(index),'status':'source-present' if manifest.is_file() else 'not-created',
            'initCommand':prefix+' init'+scope,'admitCommand':prefix+' admit'+scope,'inspectCommand':prefix+' inspect'+scope}


def project_location(realm: str, project: str, *, client=None, collection='default') -> dict[str,Any]:
    if client is None:
        with _open_client() as connected:return project_location(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    return _location(realm,project,row['content'] if row else _content(realm,project,collection),document_id,row['version'] if row else 0)


def _publish_document(client,project,document_id,row,content):
    key='project-pack-'+_json_digest([document_id,row['version'] if row else 0,content]).removeprefix('sha256:')
    result=client.documents.update(document_id,project=project,content=content,expected_version=row['version'],idempotency_key=key) if row else client.documents.create(project=project,kind=DOCUMENT_KIND,document_id=document_id,content=content,idempotency_key=key)
    if not result.ok:raise ValueError(f'Project pack document CAS rejected; source/snapshot preserved, selection not advanced: {result.error}')
    return result.data


def _index(realm,project,document_id,version,content,records):
    location=_location(realm,project,content,document_id,version)
    return {'schema':SCHEMA,'realmId':realm,'projects':[{**location,'status':'admitted'}],
            'binding':{'projectId':project,'documentId':document_id,'documentVersion':version,'contentDigest':_json_digest(content)},'elements':records}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".selection-",dir=path.parent)
    try:
        with os.fdopen(fd,"w") as stream:
            json.dump(value,stream,indent=2,sort_keys=True);stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def _verify_build(selection: dict[str,Any], path: Path) -> dict[str,Any]:
    configured=os.environ.get(BUILD_ENV)
    if not configured:raise ValueError(f'Native element build verifier is not configured; set {BUILD_ENV} through the explicit native host setup')
    try:command=json.loads(configured)
    except json.JSONDecodeError as exc:raise ValueError('Element build command must be a JSON argv array') from exc
    if not isinstance(command,list) or not command or any(not isinstance(arg,str) or not arg for arg in command):raise ValueError('Element build command must be a nonempty JSON argv array')
    fd,name=tempfile.mkstemp(prefix='build-candidate-',suffix='.json',dir=path.parent)
    candidate=Path(name)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(selection,stream,indent=2,sort_keys=True);stream.write('\n')
        try:result=subprocess.run([*command,str(candidate)],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=60,check=False)
        except (OSError,subprocess.TimeoutExpired) as exc:raise ValueError(f'Element build verifier could not finish: {exc}') from exc
        if result.returncode!=0:raise ValueError('Element build verification failed: '+(result.stderr or result.stdout)[-4096:])
        try:receipt=json.loads(result.stdout)
        except json.JSONDecodeError as exc:raise ValueError('Element build verifier omitted its structured receipt') from exc
        digest='sha256:'+hashlib.sha256(candidate.read_bytes()).hexdigest()
        if receipt.get('schema')!='astrid.element-build/v1' or receipt.get('selectionDigest')!=digest:raise ValueError('Element build receipt does not match captured selection')
        expected={(e['projectId'],*(e['descriptor'][k] for k in ('packId','id','kind','revision'))) for e in selection['elements']}
        actual={(e.get('projectId'),*(e.get(k) for k in ('packId','id','kind','revision'))) for e in receipt.get('elements',[]) if isinstance(e,dict)}
        if expected!=actual:raise ValueError('Element build receipt has missing or unexpected exact revisions')
        return receipt
    finally:candidate.unlink(missing_ok=True)


def init_project_pack(realm: str, project: str, *, client=None, collection='default') -> dict[str, Any]:
    if client is None:
        with _open_client() as connected:return init_project_pack(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    content=row['content'] if row else _content(realm,project,collection)
    location=_location(realm,project,content,document_id,row['version'] if row else 0);root=Path(location['packRoot'])
    if not root.exists():
        root.parent.mkdir(parents=True,exist_ok=True)
        temporary=Path(tempfile.mkdtemp(prefix='scaffold-',dir=root.parent))
        try:
            (temporary/'docs').mkdir()
            (temporary/'docs/SKILL.md').write_text('---\nname: project-drafts\ndescription: Ordinary project-owned draft rendering source.\n---\n\n# Project drafts\n\nEdit declared source files, validate/admit, then update exact timeline refs. Keep files while timelines use them.\n')
            pack={'schema_version':3,'id':content['pack_id'],'name':'Project drafts','version':'0.1.0','status':'active','domain':'media','stability':'experimental','support':'project','visibility':'visible','description':'Editable project-owned visual elements.','permissions':[{'id':'project_files','reason':'Provides project-owned visual source.'}],'documentation':{'kind':'skill','path':'docs/SKILL.md'},'rendering':{}}
            (temporary/'pack.yaml').write_text(yaml.safe_dump(pack,sort_keys=False))
            validate_canonical_pack(temporary,expected_pack_id=content['pack_id']);temporary.rename(root)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
    validate_canonical_pack(root,expected_pack_id=content['pack_id'])
    if row is None:
        path=Path(location['manifestPath']);path.parent.mkdir(parents=True,exist_ok=True)
        candidate=_index(realm,project,document_id,1,content,[])
        _verify_build(candidate,path)
        committed=_publish_document(client,project,document_id,None,content)
        _atomic_json(path,_index(realm,project,document_id,committed['version'],content,[]))
    return inspect_project_pack(realm,project,client=client,collection=collection)


def _resources(root: Path, *, expected_pack_id=None):
    entry=validate_canonical_pack(root,expected_pack_id=expected_pack_id)
    resources={entry.manifest.path:entry.manifest}
    resources.update({r.path:r for r in entry.resources if r.file_kind=="file"})
    if len(resources)>256 or sum(r.size for r in resources.values())>64*1024*1024:
        raise ValueError("Project pack exceeds bounded admission size")
    return entry,resources


def _check_imports(root: Path, resources: dict[str, Any]) -> None:
    for relative,handle in resources.items():
        path=root/relative
        if path.suffix not in _SOURCE_EXTENSIONS:continue
        text=path.read_text()
        if re.search(r"\b(?:import\s*\(|require\s*\(|eval\s*\(|new\s+Function\s*\()",text):
            raise ValueError(f"Unsupported dynamic import/code loading in {relative}")
        # Pack code is trusted build source, but its dependency closure is explicit.
        imports=re.findall(r"\b(?:from\s*|import\s*)(?:[\w*{},\s]+\s+from\s*)?['\"]([^'\"]+)['\"]",text)
        if path.suffix==".css":imports+=re.findall(r"@import\s+['\"]([^'\"]+)['\"]",text)
        for specifier in imports:
            if specifier in _ALLOWED_IMPORTS:continue
            if not specifier.startswith("."):
                raise ValueError(f"Unsupported dependency {specifier!r} in {relative}; supported imports: React, Remotion and declared relative resources")
            specifier=specifier.split("?",1)[0]
            target=(path.parent/specifier).resolve()
            candidates=[target,*[Path(str(target)+ext) for ext in (".ts",".tsx",".js",".jsx",".css")],target/"index.ts",target/"index.tsx"]
            if not any(c.is_relative_to(root) and c.is_file() and c.relative_to(root).as_posix() in resources for c in candidates):
                raise ValueError(f"Undeclared or escaping dependency {specifier!r} in {relative}")


def _definitions(root: Path, entry):
    definitions=[]
    for declaration in entry.definition.rendering.values():
        if declaration["type"]!="element":
            raise ValueError("Project draft pack admission supports rendering elements only")
        descriptor=root/declaration["path"]
        data=yaml.safe_load(descriptor.read_text())
        kind=data["kind"]
        definition=load_element_definition(descriptor.parent,kind=kind,source=f"pack:{entry.definition.id}",editable=True,priority=100)
        definitions.append((definition,descriptor))
    return definitions


def _selected_index(realm,project,document_id,row):
    content=row['content'];path=Path(_location(realm,project,content,document_id,row['version'])['manifestPath'])
    if not path.exists():return _index(realm,project,document_id,row['version'],content,[])
    value=json.loads(path.read_text());expected=_index(realm,project,document_id,row['version'],content,[])['binding']
    if value.get('schema')!=SCHEMA or value.get('binding')!=expected:raise ValueError('Derived pack artifact index does not match its Runtime document; rebuild admission index')
    return value


def inspect_project_pack(realm: str, project: str, *, client=None, collection='default') -> dict[str, Any]:
    if client is None:
        with _open_client() as connected:return inspect_project_pack(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    content=row['content'] if row else _content(realm,project,collection)
    location=_location(realm,project,content,document_id,row['version'] if row else 0);root=Path(location['packRoot'])
    admitted=content['admissions'];result={**location,'realmId':realm,'elements':[],'admittedRevisions':admitted}
    if row is None:result['status']='not-created' if not root.exists() else 'unbound-source-present'
    if not (root/'pack.yaml').is_file():
        if admitted:result['status']='source-missing'
        return result
    try:
        entry,resources=_resources(root);_check_imports(root,resources)
        for definition,descriptor_path in _definitions(root,entry):
            descriptor=_element_descriptor(definition)
            matching=[e for e in admitted if all(e.get(k)==descriptor.get(k) for k in ('id','kind','revision'))]
            result['elements'].append({'descriptor':descriptor,'authoring':{'packRoot':str(root),'descriptorPath':str(descriptor_path),'componentPath':str(definition.component),'status':'admitted' if matching else 'not-yet-admitted'}})
        result['status']='admitted' if result['elements'] and all(e['authoring']['status']=='admitted' for e in result['elements']) else 'not-yet-admitted'
    except (ValueError,OSError,KeyError) as exc:result['status']='source-invalid';result['error']=str(exc)
    return result


def admit_project_pack(realm: str, project: str, *, client=None, collection='default') -> dict[str, Any]:
    if client is None:
        with _open_client() as connected:return admit_project_pack(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    if row is None:raise ValueError('Project pack binding is not initialized; run project-pack init')
    content=row['content'];location=_location(realm,project,content,document_id,row['version']);root=Path(location['packRoot'])
    if root.is_symlink():raise ValueError("Project pack root is a symlink")
    root=root.resolve()
    if not (root/"pack.yaml").is_file():raise ValueError(f"Project pack is not created. Run: {location['initCommand']}")
    entry,resources=_resources(root);_check_imports(root,resources)
    if entry.definition.id!=location["packId"]:raise ValueError("Project pack owner does not match configured project binding")
    definitions=_definitions(root,entry)
    if not definitions:raise ValueError("Project pack has no declared elements to admit")
    for definition,_ in definitions:
        if definition.component.relative_to(root).as_posix() not in resources:raise ValueError("Element implementation must be declared in pack resources")
        required=[*definition.support_files,*[(definition.root/asset.path).resolve() for asset in definition.assets]]
        if any(not resource.is_relative_to(root) or resource.relative_to(root).as_posix() not in resources for resource in required):raise ValueError('Element asset/support closure must be declared in pack resources')

        for helper in definition.root.rglob("*"):
            if helper.is_file() and helper.suffix in _SOURCE_EXTENSIONS and helper.relative_to(root).as_posix() not in resources:raise ValueError(f"Element helper is undeclared: {helper}")
    files=[{"path":p,"sha256":r.sha256} for p,r in sorted(resources.items())]
    closure_hash=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    snapshot_root=artifact_root()/realm/project/entry.definition.id/"snapshots"/closure_hash
    snapshot_root.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix="capture-",dir=snapshot_root.parent))
    try:
        for item in files:
            target=staging/item["path"];target.parent.mkdir(parents=True,exist_ok=True)
            payload=(root/item["path"]).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=item["sha256"]:raise ValueError("Source changed during admission; inspect and admit again")
            target.write_bytes(payload)
        # Revalidate after capture; the canonical source cannot silently move mid-copy.
        _,confirmed=_resources(root)
        if {p:r.sha256 for p,r in confirmed.items()}!={p:r.sha256 for p,r in resources.items()}:raise ValueError("Source changed during admission")
        validate_canonical_pack(staging,expected_pack_id=entry.definition.id)
        if snapshot_root.exists():
            for item in files:
                saved=snapshot_root/item["path"]
                if saved.is_symlink() or not saved.is_file() or hashlib.sha256(saved.read_bytes()).hexdigest()!=item["sha256"]:raise ValueError("Existing admitted snapshot is corrupt; refusing overwrite")
        else:
            staging.rename(snapshot_root)
        records=[]
        for definition,descriptor_path in definitions:
            descriptor=_element_descriptor(definition)
            component=definition.component.relative_to(root).as_posix()
            descriptor["componentPath"]=f"external/{entry.definition.id}/{closure_hash}/{component}"
            records.append({"projectId":project,"descriptor":descriptor,
                "authoring":{"packRoot":str(root),"descriptorPath":str(descriptor_path),"componentPath":str(definition.component),"status":"admitted"},
                "snapshot":{"root":str(snapshot_root),"componentPath":component,"files":files}})
        previous=_selected_index(realm,project,document_id,row)
        admissions=[dict(a) for a in content['admissions']]
        for record in records:
            descriptor=record['descriptor'];admission={k:descriptor[k] for k in ('id','kind','revision')};admission['snapshot_digest']=closure_hash
            if admission not in admissions:admissions.append(admission)
        next_content={**content,'admissions':admissions};path=Path(location['manifestPath']);path.parent.mkdir(parents=True,exist_ok=True)
        # Only changed pack inputs are compiled. Existing immutable revisions
        # are retained in the derived index and Runtime ref inventory unchanged.
        build_candidate=_index(realm,project,document_id,row['version']+1,next_content,records)
        build_receipt=_verify_build(build_candidate,path)
        _atomic_json(snapshot_root/'.build-receipt.json',build_receipt)
        committed=_publish_document(client,project,document_id,row,next_content) if next_content!=content else row
        selected_keys={(e['descriptor']['id'],e['descriptor']['kind'],e['descriptor']['revision']) for e in records}
        retained=[e for e in previous['elements'] if (e['descriptor']['id'],e['descriptor']['kind'],e['descriptor']['revision']) not in selected_keys]
        selected=_index(realm,project,document_id,committed['version'],next_content,[*retained,*records])
        _atomic_json(path,selected)
        return {'ok':True,'manifestPath':str(path),'documentId':document_id,'documentVersion':committed['version'],'project':{**location,'status':'admitted'},'elements':records,'instruction':'Pack files compiled and selected by Runtime document CAS. Publish the exact same timeline placement at its captured head, then refresh the editor. Admission alone did not edit a timeline.'}
    finally:
        if staging.exists():shutil.rmtree(staging)


def _record_from_admission(realm,project,content,admission):
    root=artifact_root()/realm/project/content['pack_id']/'snapshots'/admission['snapshot_digest']
    entry,resources=_resources(root,expected_pack_id=content['pack_id'])
    files=[{'path':p,'sha256':r.sha256} for p,r in sorted(resources.items())]
    if _json_digest(files).removeprefix('sha256:')!=admission['snapshot_digest']:raise ValueError('Selected snapshot resource closure is corrupt')
    matches=[(d,p) for d,p in _definitions(root,entry) if d.id==admission['id'] and _element_descriptor(d)['kind']==admission['kind']]
    if len(matches)!=1:raise ValueError('Selected pack element is unavailable')
    definition,path=matches[0];descriptor=_element_descriptor(definition)
    if descriptor['revision']!=admission['revision']:raise ValueError('Selected pack source revision mismatch')
    source_root=Path(_location(realm,project,content,'derived',0)['packRoot'])
    relative_component=definition.component.relative_to(root).as_posix()
    descriptor['componentPath']=f"external/{content['pack_id']}/{admission['snapshot_digest']}/{relative_component}"
    return {'projectId':project,'descriptor':descriptor,'authoring':{'packRoot':str(source_root),'descriptorPath':str(source_root/path.relative_to(root)),'componentPath':str(source_root/relative_component),'status':'admitted'},'snapshot':{'root':str(root),'componentPath':relative_component,'files':files}}


def materialize_project_pack(realm,project,*,client=None,collection='default'):
    if client is None:
        with _open_client() as connected:return materialize_project_pack(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    if row is None:raise ValueError('Project pack document is not initialized')
    records=[_record_from_admission(realm,project,row['content'],a) for a in row['content']['admissions']]
    value=_index(realm,project,document_id,row['version'],row['content'],records)
    path=Path(_location(realm,project,row['content'],document_id,row['version'])['manifestPath']);_atomic_json(path,value)
    return {'ok':True,'manifestPath':str(path),'documentId':document_id,'documentVersion':row['version'],'elements':records}


def resolve_admitted_element(realm: str, project: str, reference: dict[str,str], *, client=None) -> dict[str,Any]:
    if client is None:
        with _open_client() as connected:return resolve_admitted_element(realm,project,reference,client=connected)
    required={'id','kind','packId','revision'}
    if not required.issubset(reference) or any(not isinstance(reference[k],str) or not reference[k] for k in required):raise ValueError('An admitted owner-qualified element reference is required')
    document_id,row=_read_document(client,realm,project)
    if row is None or row['content']['pack_id']!=reference['packId']:
        document_id,row=_read_document(client,realm,project,reference['packId'])
    if row is None or row['content']['pack_id']!=reference['packId']:raise ValueError('Element owner/revision is not admitted for this project')
    admitted=[a for a in row['content']['admissions'] if all(a.get(k)==reference[k] for k in ('id','kind','revision'))]
    if len(admitted)!=1:raise ValueError('Element owner/revision is not admitted for this project')
    record=_record_from_admission(realm,project,row['content'],admitted[0])
    snapshot=record['snapshot'];root=Path(snapshot['root'])
    expected_root=artifact_root()/realm/project/reference['packId']/'snapshots'/admitted[0]['snapshot_digest']
    if root!=expected_root:raise ValueError('Artifact snapshot is outside its selected Runtime binding')
    entry,resources=_resources(root,expected_pack_id=reference['packId'])
    files=[{'path':p,'sha256':r.sha256} for p,r in sorted(resources.items())]
    digest=_json_digest(files).removeprefix('sha256:')
    if digest!=admitted[0]['snapshot_digest'] or files!=snapshot['files']:raise ValueError('Admitted element snapshot is missing or corrupt')
    build=json.loads((root/'.build-receipt.json').read_text())
    identity={'projectId':project,**{k:reference[k] for k in ('packId','id','kind','revision')}}
    if build.get('schema')!='astrid.element-build/v1' or identity not in build.get('elements',[]):raise ValueError('Selected source lacks a successful build receipt')
    exact=[_element_descriptor(d) for d,_ in _definitions(root,entry) if d.id==reference['id']]
    if len(exact)!=1 or any(exact[0].get(k)!=reference[k] for k in required):raise ValueError('Admitted source does not match canonical revision')
    return {**record,'binding':_index(realm,project,document_id,row['version'],row['content'],[])['binding']}


def configured_realm() -> str:
    """Read public launcher discovery, never credentials or preference prose."""
    discovery=os.environ.get('ASTRID_WORKSPACE_DISCOVERY')
    if not discovery:raise ValueError('Pass --realm or configure ASTRID_WORKSPACE_DISCOVERY')
    value=json.loads(Path(discovery).read_text())
    return _identity(value.get('active_realm') or value.get('realm_id') or '')


def timeline_element_authoring(realm: str, project: str, timeline: str, bundle: dict[str,Any], *, client=None) -> dict[str,Any]:
    if client is None:
        with _open_client() as connected:return timeline_element_authoring(realm,project,timeline,bundle,client=connected)
    context=inspect_project_pack(realm,project,client=client)
    rows=[]
    parent=bundle.get('parent') or {}
    for clip in parent.get('clips') or []:
        ref=clip.get('elementRef') or clip.get('element_ref')
        if not isinstance(ref,dict):continue
        try:admitted=[resolve_admitted_element(realm,project,ref,client=client)] if ref.get('packId') else []
        except (ValueError,OSError):admitted=[]
        current=[e for e in context.get('elements',[]) if all(e['descriptor'].get(k)==ref.get(k) for k in ('packId','kind','id'))]
        row={'clipId':clip.get('id'),'element':ref,'at':clip.get('at'),'duration':clip.get('hold'),'track':clip.get('track'),'params':clip.get('params',{}),'status':'legacy-migration-required' if not ref.get('packId') else 'not-admitted'}
        if admitted:
            record=admitted[0]
            row.update({'status':'admitted','authoring':record['authoring'],'schema':record['descriptor']['schema'],'defaults':record['descriptor']['defaults']})
            if current:row['authoring']={**current[0]['authoring'],'currentRevision':current[0]['descriptor']['revision']}
            elif not Path(record['authoring']['componentPath']).is_file():row['status']='source-missing-snapshot-available'
            row['updateCommand']=f"astrid-tools timelines update-element --project {shlex.quote(project)} {shlex.quote(timeline)} --realm {shlex.quote(realm)} --clip-id {shlex.quote(str(clip.get('id')))} --pack {shlex.quote(ref['packId'])} --element {shlex.quote(ref['id'])} --kind {shlex.quote(ref['kind'])} --revision {shlex.quote(ref['revision'])} --expected-head {shlex.quote(str(bundle.get('base_parent',{}).get('revision_id')))}"
        rows.append(row)
    return {'projectPack':context,'timelineId':timeline,'expectedHead':bundle.get('base_parent',{}).get('revision_id'),'elements':rows,'instruction':'Read/edit ordinary pack files, validate/admit, then publish the same placement using update-element and the exact captured head. Only a durable publication receipt means saved.'}
