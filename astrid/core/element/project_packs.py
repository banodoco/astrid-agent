"""Project draft packs: ordinary author files and immutable admitted build inputs.

This is local source selection/configuration, never a second project/timeline store.
Reads are observational; only explicit init/admit creates files.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

import yaml

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.sdk.documents import stable_document_id

from .catalog import _element_descriptor
from .schema import element_definition_from_payload, load_element_definition

SCHEMA = "astrid.element-admissions/v1"
ROOT_ENV = "ASTRID_PROJECT_PACKS_ROOT"
ARTIFACT_ENV = "ASTRID_ELEMENT_ARTIFACT_ROOT"
DOCUMENT_KIND = "astrid.project-pack"
DOCUMENT_SCHEMA = "astrid.project-pack/v1"
SOURCE_BINDING = "project-packs"
_ALLOWED_IMPORTS = {"react", "react/jsx-runtime", "remotion"}
_SOURCE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".css"}


class _ElementRecord(TypedDict):
    projectId: str
    descriptor: dict[str, object]
    authoring: dict[str, str]
    snapshot: dict[str, object]


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
    _project_pack_content(row)

    return document_id,row


def _project_pack_content(row):
    content=row.get('content')
    if not isinstance(content,dict) or content.get('schema')!=DOCUMENT_SCHEMA or content.get('source_binding_key')!=SOURCE_BINDING:raise ValueError('Unsupported project pack payload schema/source binding')
    if not re.fullmatch(r'[a-z][a-z0-9_]*',str(content.get('pack_id',''))):raise ValueError('Invalid project pack owner')
    if set(content)!={'schema','pack_id','source_binding_key','relative_manifest','admissions'} or not isinstance(content['admissions'],list):raise ValueError('Invalid project pack payload fields')
    for admission in content['admissions']:
        if not isinstance(admission,dict) or set(admission)-{'snapshot_object','compiled','descriptor','authoring'}!={'id','kind','revision','snapshot_digest'} or admission['kind'] not in {'effect','animation','transition'} or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(admission['revision'])) or not re.fullmatch(r'[0-9a-f]{64}',str(admission['snapshot_digest'])):raise ValueError('Invalid selected pack admission')
        if 'snapshot_object' in admission:
            _snapshot_descriptor(admission['snapshot_object'])
        if 'compiled' in admission:
            compiled=admission['compiled']
            if (not isinstance(compiled,dict) or set(compiled)!={'path','sha256'}
                    or not isinstance(compiled['path'],str) or not re.fullmatch(r'[0-9a-f]{64}',str(compiled['sha256']))):
                raise ValueError('Invalid compiled element descriptor')
        if 'descriptor' in admission and not isinstance(admission['descriptor'],dict):raise ValueError('Invalid admitted element descriptor')
        if 'authoring' in admission and not isinstance(admission['authoring'],dict):raise ValueError('Invalid admitted element authoring')

    return content


def _snapshot_descriptor(value):
    if (
        not isinstance(value, Mapping)
        or set(value) != {'object_id', 'digest', 'size', 'media_type'}
        or not isinstance(value.get('digest'), str)
        or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['digest'])
        or value.get('object_id') != value['digest']
        or type(value.get('size')) is not int
        or value['size'] <= 0
        or value.get('media_type') != 'application/json'
    ):
        raise ValueError('Invalid pack snapshot object descriptor')
    return dict(value)


def _closure_digest(files):
    # Admission predates UTF-8 Document digests; retain its ASCII escaping.
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate pack snapshot JSON key')
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('Non-finite pack snapshot JSON value')

    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


def _decode_base64(value):
    if not isinstance(value, str):
        raise ValueError('Invalid pack snapshot base64')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Invalid pack snapshot base64') from exc
    if base64.b64encode(raw).decode('ascii') != value:
        raise ValueError('Noncanonical pack snapshot base64')
    return raw


def _snapshot_resources(data):
    value = _strict_json(data)
    if (not isinstance(value, dict) or set(value) != {'files', 'build_receipt_base64'}
            or not isinstance(value['files'], list) or not 0 < len(value['files']) <= 256):
        raise ValueError('Invalid bounded pack snapshot envelope')
    if json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                  allow_nan=False).encode('utf-8') != data:
        raise ValueError('Noncanonical pack snapshot JSON')
    files = {}
    total = 0
    for item in value['files']:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'bytes_base64'}:
            raise ValueError('Invalid pack snapshot resource')
        name = item['path']
        if (not isinstance(name, str) or '\\' in name or '\x00' in name
                or any(part in ('', '.', '..') for part in name.split('/'))
                or name in files):
            raise ValueError('Invalid or duplicate pack snapshot resource path')
        raw = _decode_base64(item['bytes_base64'])
        total += len(raw)
        if total > 64 * 1024 * 1024:
            raise ValueError('Invalid bounded pack snapshot resources')
        if item['sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('Pack snapshot resource hash mismatch')
        files[name] = raw
    if list(files) != sorted(files):
        raise ValueError('Pack snapshot resources must be path-sorted')
    receipt = _decode_base64(value['build_receipt_base64'])
    _strict_json(receipt)
    return files, receipt


def _portable_snapshot(files, receipt):
    value = {'files': [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                        'bytes_base64': base64.b64encode(raw).decode('ascii')}
                       for name, raw in sorted(files.items())],
             'build_receipt_base64': base64.b64encode(receipt).decode('ascii')}
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')
    _snapshot_resources(data)
    return data


def _snapshot_object_resources(descriptor, data):
    _snapshot_descriptor(descriptor)
    if (not isinstance(data, bytes) or len(data) != descriptor['size']
            or 'sha256:' + hashlib.sha256(data).hexdigest() != descriptor['digest']):
        raise ValueError('Pack snapshot object bytes do not match descriptor')
    return _snapshot_resources(data)


def _ingest_snapshot(client, project, data):
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    expected = {'object_id': digest, 'digest': digest, 'size': len(data),
                'media_type': 'application/json'}
    result = client.media.import_bytes(project=project, data=data, filename='pack-snapshot.json',
                                       media_type='application/json',
                                       idempotency_key='project-pack-snapshot-' + _json_digest(
                                           [project, digest]).removeprefix('sha256:'))
    if not result.ok or not isinstance(result.data, Mapping):
        raise ValueError('Project pack snapshot ingest failed; selection not advanced')
    actual = {key: result.data.get(key) for key in expected}
    if _snapshot_descriptor(actual) != expected:
        raise ValueError('Project pack snapshot ingest metadata mismatch')
    return actual


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
    _atomic_bytes(path, (json.dumps(value,indent=2,sort_keys=True)+'\n').encode('utf-8'))


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".selection-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def _build_receipt(selection: dict[str,Any]) -> dict[str,Any]:
    """The build record stored with the snapshot: the exact selection every element compiled under.

    The compile is the verification (``_compile_components`` runs before this), so nothing else is checked.
    """
    data = (json.dumps(selection,indent=2,sort_keys=True)+'\n').encode('utf-8')
    return {'schema':'astrid.element-build/v1','selectionDigest':'sha256:'+hashlib.sha256(data).hexdigest(),
            'elements':[{'projectId':e['projectId'],**{k:e['descriptor'][k] for k in ('packId','id','kind','revision')}}
                        for e in selection['elements']],
            'compiledBindings':[{'projectId':e['projectId'],**{k:e['descriptor'][k] for k in ('packId','id','kind','revision')},
                                 'componentPath':e['snapshot']['componentPath'],'compiled':e['compiled']}
                                for e in selection['elements']]}


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
            from astrid.pack_api import VERSION as PACK_API_VERSION
            pack={'schema_version':3,'id':content['pack_id'],'name':'Project drafts','version':'0.1.0','pack_api':'%d.%d'%PACK_API_VERSION,'status':'active','domain':'media','stability':'experimental','support':'project','visibility':'visible','description':'Editable project-owned visual elements.','permissions':[{'id':'project_files','reason':'Provides project-owned visual source.'}],'documentation':{'kind':'skill','path':'docs/SKILL.md'},'rendering':{}}
            (temporary/'pack.yaml').write_text(yaml.safe_dump(pack,sort_keys=False))
            validate_canonical_pack(temporary,expected_pack_id=content['pack_id']);temporary.rename(root)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
    validate_canonical_pack(root,expected_pack_id=content['pack_id'])
    if row is None:
        path=Path(location['manifestPath']);path.parent.mkdir(parents=True,exist_ok=True)
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


COMPILED_DIR='compiled'


def _compiled_path(component: str) -> str:
    return f'{COMPILED_DIR}/{component}.mjs'


def _closure(root: Path, entry, resources) -> list[dict[str,str]]:
    """Source resources plus one compiled module per element component, path-sorted."""
    components=[definition.component.relative_to(root).as_posix() for definition,_ in _definitions(root,entry)]
    files=[{'path':p,'sha256':r.sha256} for p,r in resources.items()]
    for component in sorted(set(components)):
        name=_compiled_path(component);target=root/name
        if target.is_symlink() or not target.is_file():
            raise ValueError(f'Compiled element module {name} is missing from the admitted snapshot; re-admit the pack')
        files.append({'path':name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    return sorted(files,key=lambda item:item['path'])


def _compiled_ref(files, component: str) -> dict[str,str]:
    name=_compiled_path(component)
    return {'path':name,'sha256':next(item['sha256'] for item in files if item['path']==name)}


_REPAIR = "Repair the install: run the Astrid install command again without --update."


def _esbuild_pin(project: Path) -> str:
    """The exact esbuild version the Remotion project pins in its own package.json."""
    package = project/'package.json'
    try:
        pinned = json.loads(package.read_text(encoding='utf-8'))['dependencies']['esbuild']
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ValueError(f'Project element admission needs the Remotion esbuild pin, but {package} does not declare esbuild. {_REPAIR}') from exc
    if not isinstance(pinned,str) or not re.fullmatch(r'\d+\.\d+\.\d+',pinned):
        raise ValueError(f'{package} must pin esbuild to one exact version, not {pinned!r}')
    return pinned


_ESBUILD_VERSION_PROBE = "process.stdout.write(require(process.argv[1]).version)"


def _renderer_compiler() -> tuple[Path,Path]:
    """The renderer's own Node and the Remotion project's esbuild JS API, on any platform.

    Node follows the Remotion project's engines pin (``ASTRID_NODE_EXECUTABLE`` must match it when set).
    esbuild is the project's own package, loaded through Node (``node_modules/esbuild/lib/main.js``): the API
    finds its own native binary, so a JS shim at ``bin/esbuild`` or a Windows ``.exe`` layout both work.
    Its version is checked against the project's exact pin. No separate lock, no PATH.
    """
    from astrid.core.env_vars import ASTRID_NODE_EXECUTABLE, ASTRID_REMOTION_PROJECT_DIR
    from astrid.core.foundation.renderer_toolchain import (
        resolve_pinned_node,
        resolve_remotion_project_dir,
    )

    project = resolve_remotion_project_dir(ASTRID_REMOTION_PROJECT_DIR, repo_root=REPO_ROOT)
    node = resolve_pinned_node(project,override=(os.environ.get(ASTRID_NODE_EXECUTABLE) or '').strip() or None)
    if not node.ok:
        raise ValueError(f'Project element admission needs the renderer Node: {node.problem}')
    pin = _esbuild_pin(project)
    api = project/'node_modules'/'esbuild'/'lib'/'main.js'
    if not api.is_file():
        raise ValueError(f'Project element admission needs the Remotion esbuild {pin}, but {api} is missing. {_REPAIR}')
    try:
        probe = subprocess.run([str(node.path),'-e',_ESBUILD_VERSION_PROBE,str(api)],
                               stdin=subprocess.DEVNULL,capture_output=True,text=True,
                               env=_node_env(),timeout=15,check=False)
    except (OSError,subprocess.SubprocessError) as exc:
        raise ValueError(f'The Remotion esbuild {api} cannot run ({exc}). {_REPAIR}') from exc
    observed = probe.stdout.strip() if probe.returncode == 0 else ''
    if observed != pin:
        raise ValueError(f'The Remotion esbuild {api} reports {observed or "nothing"}, but {project/"package.json"} pins {pin}. {_REPAIR}')
    return node.path, api


def _node_env() -> dict[str,str]:
    """An empty environment for Node, plus the Windows variables it cannot start without."""
    env = {'PATH':'','LANG':'C.UTF-8'}
    env.update({k:os.environ[k] for k in ('SYSTEMROOT','SystemRoot') if k in os.environ})
    return env


def _compile_components(staging: Path, components) -> None:
    from astrid.pack_build import compile_element_module
    node, api = _renderer_compiler()
    for component in sorted(set(components)):
        target=staging/_compiled_path(component);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(compile_element_module(node,api,staging,component,_node_env()))


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
    components=[definition.component.relative_to(root).as_posix() for definition,_ in definitions]
    if any(_compiled_path(component) in resources for component in components):raise ValueError("Compiled element path collides with a source file")
    snapshots=artifact_root()/realm/project/entry.definition.id/"snapshots"
    snapshots.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix="capture-",dir=snapshots))
    try:
        for relative,handle in sorted(resources.items()):
            target=staging/relative;target.parent.mkdir(parents=True,exist_ok=True)
            payload=(root/relative).read_bytes()
            if hashlib.sha256(payload).hexdigest()!=handle.sha256:raise ValueError("Source changed during admission; inspect and admit again")
            target.write_bytes(payload)
        # Revalidate after capture; the canonical source cannot silently move mid-copy.
        _,confirmed=_resources(root)
        if {p:r.sha256 for p,r in confirmed.items()}!={p:r.sha256 for p,r in resources.items()}:raise ValueError("Source changed during admission")
        validate_canonical_pack(staging,expected_pack_id=entry.definition.id)
        # Each component also compiles to one ESM module; that file joins the closure.
        # The raw TSX stays in the snapshot for render.
        _compile_components(staging,components)
        files=_closure(staging,entry,resources)
        source_digest=_closure_digest([item for item in files if item['path'] in resources])
        for definition,_ in definitions:
            descriptor=_element_descriptor(definition)
            matching=[a for a in content['admissions'] if all(a[k]==descriptor[k] for k in ('id','kind','revision'))]
            if any('compiled' not in a and a['snapshot_digest']!=source_digest for a in matching):
                raise ValueError('Legacy selected pack admission source closure changed; restore its original pack resources before re-admitting')
        closure_hash=_closure_digest(files)
        snapshot_root=snapshots/closure_hash
        if snapshot_root.exists():
            for item in files:
                saved=snapshot_root/item["path"]
                if saved.is_symlink() or not saved.is_file() or hashlib.sha256(saved.read_bytes()).hexdigest()!=item["sha256"]:raise ValueError("Existing admitted snapshot is corrupt; refusing overwrite")
        else:
            staging.rename(snapshot_root)
        records: list[_ElementRecord] = []
        for definition,descriptor_path in definitions:
            descriptor=_element_descriptor(definition)
            component=definition.component.relative_to(root).as_posix()
            descriptor["componentPath"]=f"external/{entry.definition.id}/{closure_hash}/{component}"
            records.append({"projectId":project,"descriptor":descriptor,
                "authoring":{"packRoot":str(root),"descriptorPath":str(descriptor_path),"componentPath":str(definition.component),"status":"admitted"},
                "snapshot":{"root":str(snapshot_root),"componentPath":component,"files":files},
                "compiled":_compiled_ref(files,component)})
        previous=_selected_index(realm,project,document_id,row)
        admissions=[dict(a) for a in content['admissions']]
        build_records=list(records)
        selected_keys=set();upgraded=set()
        for index,record in enumerate(records):
            descriptor=record['descriptor'];admission={k:descriptor[k] for k in ('id','kind','revision')};admission['snapshot_digest']=closure_hash
            matching=[a for a in admissions if all(a[k]==admission[k] for k in ('id','kind','revision'))]
            if len(matching)>1:
                raise ValueError('Ambiguous selected pack admission')
            if matching and 'compiled' not in matching[0]:
                # A pre-compiled-module admission with its original source closure, upgraded in place to the new
                # closure and compiled file. Its snapshot object is republished under the new digest.
                new=dict(admission,descriptor=descriptor,compiled=record['compiled'],
                    authoring={'packRoot':str(root),'descriptorPath':Path(record['authoring']['descriptorPath']).relative_to(root).as_posix(),
                               'componentPath':record['snapshot']['componentPath'],'status':'admitted'})
                admissions[admissions.index(matching[0])]=new
                upgraded.add((admission['id'],admission['kind'],admission['revision']))
                selected_keys.add((admission['id'],admission['kind'],admission['revision']))
                continue
            if matching:
                if 'snapshot_object' not in matching[0] and matching[0]['snapshot_digest']!=closure_hash:
                    raise ValueError('Selected pack admission closure changed; cannot replace immutable snapshot')
                retained=next((e for e in previous['elements'] if all(
                    e['descriptor'][k]==admission[k] for k in ('id','kind','revision'))),None)
                records[index]=retained if retained is not None else _record_from_admission(realm,project,content,matching[0])
            else:
                # Runtime's exact listing shape: packRoot is the authoring root; the element paths are pack-relative.
                admission.update(descriptor=descriptor,compiled=record['compiled'],
                    authoring={'packRoot':str(root),'descriptorPath':Path(record['authoring']['descriptorPath']).relative_to(root).as_posix(),
                               'componentPath':record['snapshot']['componentPath'],'status':'admitted'})
                admissions.append(admission)
            if not matching or matching[0]['snapshot_digest']==closure_hash:
                selected_keys.add((admission['id'],admission['kind'],admission['revision']))
        next_content={**content,'admissions':admissions};path=Path(location['manifestPath']);path.parent.mkdir(parents=True,exist_ok=True)
        # Verify the full current pack, but attach its closure only to new
        # revisions or legacy admissions whose original closure still matches.
        build_content={**next_content,'admissions':[
            {k:v for k,v in a.items() if k!='snapshot_object'}
            if (a['id'],a['kind'],a['revision']) in selected_keys else a for a in admissions]}
        build_candidate=_index(realm,project,document_id,row['version']+1,build_content,build_records)
        build_receipt=_build_receipt(build_candidate)
        existing=[a['snapshot_object'] for a in admissions
                  if (a['id'],a['kind'],a['revision']) in selected_keys and 'snapshot_object' in a]
        receipt_path=snapshot_root/'.build-receipt.json'
        if receipt_path.is_symlink():
            raise ValueError('Pack build receipt is a symlink')
        if not receipt_path.exists():
            if existing:
                _,original_receipt=_snapshot_object_resources(
                    existing[0],client.media.read_bytes(existing[0]['object_id']))
                _atomic_bytes(receipt_path,original_receipt)
            else:
                _atomic_json(receipt_path,build_receipt)
        receipt=receipt_path.read_bytes()
        snapshot_files={item['path']:(snapshot_root/item['path']).read_bytes() for item in files}
        for record in build_records:
            reference={k:record['descriptor'][k] for k in ('packId','id','kind','revision')}
            if (reference['id'],reference['kind'],reference['revision']) not in selected_keys:
                continue
            admission=next(a for a in admissions if all(a[k]==reference[k] for k in ('id','kind','revision')))
            _admitted_snapshot_descriptor(project,reference,build_content,admission,snapshot_files,receipt)
        portable=_portable_snapshot(snapshot_files,receipt)
        expected={'object_id':'sha256:'+hashlib.sha256(portable).hexdigest(),
                  'digest':'sha256:'+hashlib.sha256(portable).hexdigest(),
                  'size':len(portable),'media_type':'application/json'}
        if any(value!=expected for value in existing):
            raise ValueError('Published pack snapshot differs from captured resources/build receipt')
        if any('snapshot_object' not in a for a in admissions
               if (a['id'],a['kind'],a['revision']) in selected_keys):
            snapshot_object=_ingest_snapshot(client,project,portable)
            for admission in admissions:
                if (admission['id'],admission['kind'],admission['revision']) in selected_keys and 'snapshot_object' not in admission:
                    admission['snapshot_object']=snapshot_object
        committed=_publish_document(client,project,document_id,row,next_content) if next_content!=content else row
        previous_elements=[e for e in previous['elements'] if (e['descriptor']['id'],e['descriptor']['kind'],e['descriptor']['revision']) not in upgraded]
        retained_keys={(e['descriptor']['id'],e['descriptor']['kind'],e['descriptor']['revision']) for e in previous_elements}
        added=[e for e in records if (e['descriptor']['id'],e['descriptor']['kind'],e['descriptor']['revision']) not in retained_keys]
        selected=_index(realm,project,document_id,committed['version'],next_content,[*previous_elements,*added])
        _atomic_json(path,selected)
        return {'ok':True,'manifestPath':str(path),'documentId':document_id,'documentVersion':committed['version'],'project':{**location,'status':'admitted'},'elements':records,'instruction':'Pack files compiled and selected by Runtime document CAS. Publish the exact same timeline placement at its captured head, then refresh the editor. Admission alone did not edit a timeline.'}
    finally:
        if staging.exists():shutil.rmtree(staging)


def _record_from_admission(realm,project,content,admission):
    if 'compiled' not in admission:raise ValueError('Admitted element predates compiled modules; re-admit the pack')
    root=artifact_root()/realm/project/content['pack_id']/'snapshots'/admission['snapshot_digest']
    entry,resources=_resources(root,expected_pack_id=content['pack_id'])
    files=_closure(root,entry,resources)
    if _closure_digest(files)!=admission['snapshot_digest']:raise ValueError('Selected snapshot resource closure is corrupt')
    matches=[(d,p) for d,p in _definitions(root,entry) if d.id==admission['id'] and _element_descriptor(d)['kind']==admission['kind']]
    if len(matches)!=1:raise ValueError('Selected pack element is unavailable')
    definition,path=matches[0];descriptor=_element_descriptor(definition)
    if descriptor['revision']!=admission['revision']:raise ValueError('Selected pack source revision mismatch')
    source_root=Path(_location(realm,project,content,'derived',0)['packRoot'])
    relative_component=definition.component.relative_to(root).as_posix()
    descriptor['componentPath']=f"external/{content['pack_id']}/{admission['snapshot_digest']}/{relative_component}"
    return {'projectId':project,'descriptor':descriptor,'authoring':{'packRoot':str(source_root),'descriptorPath':str(source_root/path.relative_to(root)),'componentPath':str(source_root/relative_component),'status':'admitted'},'snapshot':{'root':str(root),'componentPath':relative_component,'files':files},'compiled':_compiled_ref(files,relative_component)}


def materialize_project_pack(realm,project,*,client=None,collection='default'):
    if client is None:
        with _open_client() as connected:return materialize_project_pack(realm,project,client=connected,collection=collection)
    document_id,row=_read_document(client,realm,project,collection)
    if row is None:raise ValueError('Project pack document is not initialized')
    records=[_record_from_admission(realm,project,row['content'],a) for a in row['content']['admissions']]
    value=_index(realm,project,document_id,row['version'],row['content'],records)
    path=Path(_location(realm,project,row['content'],document_id,row['version'])['manifestPath']);_atomic_json(path,value)
    return {'ok':True,'manifestPath':str(path),'documentId':document_id,'documentVersion':row['version'],'elements':records}


def admitted_element_descriptor(realm, project, reference, verified, files, build_receipt):
    """Resolve an exact declaration from Runtime-verified, retained inputs.

    ``verified`` is the existing VerifiedDocument. ``files`` contains only the
    immutable CAS resource bytes, not a caller catalog or an inline schema.
    The port must independently verify custody and current admission authority.
    This resolver checks their exact identity/content without external reads.
    """
    _identity(realm)
    _identity(project)
    required = {"id", "kind", "packId", "revision"}
    if (
        not isinstance(reference, dict)
        or not required.issubset(reference)
        or any(not isinstance(reference[k], str) or not reference[k] for k in required)
    ):
        raise ValueError("An admitted owner-qualified element reference is required")
    if not isinstance(verified, Mapping) or not isinstance(verified.get("document"), Mapping):
        raise ValueError("Missing verified project pack declaration")
    row = verified["document"]
    pin = verified.get("pin")
    if (
        row.get("kind") != DOCUMENT_KIND
        or row.get("project_id") != project
        or type(row.get("version")) is not int
        or row["version"] < 1
        or pin
        != {
            "document_id": row.get("document_id"),
            "version": row["version"],
            "content_digest": _json_digest(row.get("content")),
        }
    ):
        raise ValueError("Project pack declaration does not match its exact scoped pin")
    content = _project_pack_content(row)
    owner = content["pack_id"]
    collection = "default" if owner == _content(realm, project)["pack_id"] else owner
    if (
        owner != reference["packId"]
        or row.get("document_id") != stable_document_id(project, DOCUMENT_KIND, collection)
        or content["relative_manifest"] != f"{realm}/{project}/packs/{owner}/pack.yaml"
    ):
        raise ValueError("Project pack owner or scope does not match selected declaration")
    admitted = [
        a
        for a in content["admissions"]
        if all(a.get(k) == reference[k] for k in ("id", "kind", "revision"))
    ]
    if len(admitted) != 1:
        raise ValueError("Element owner/revision is not admitted for this project")
    return _admitted_snapshot_descriptor(project, reference, content, admitted[0], files, build_receipt)


def _admitted_snapshot_descriptor(project, reference, content, admission, files, build_receipt):
    owner = content['pack_id']
    required = {'id', 'kind', 'packId', 'revision'}
    if (
        not isinstance(files, Mapping)
        or not files
        or len(files) > 256
        or any(
            not isinstance(name, str) or not isinstance(value, bytes)
            for name, value in files.items()
        )
        or sum(map(len, files.values())) > 64 * 1024 * 1024
    ):
        raise ValueError("Invalid bounded admitted snapshot resources")
    files = dict(files)
    for name in files:
        if (
            name.startswith("/")
            or any(part in ("", "..", ".") for part in name.split("/"))
            or "\\" in name
        ):
            raise ValueError("Invalid admitted snapshot resource path")
    closure = [
        {"path": name, "sha256": hashlib.sha256(value).hexdigest()}
        for name, value in sorted(files.items())
    ]
    if _closure_digest(closure) != admission["snapshot_digest"]:
        raise ValueError("Admitted element snapshot is missing or corrupt")
    build = _strict_json(build_receipt)
    identity = {
        "projectId": project,
        **{k: reference[k] for k in ("packId", "id", "kind", "revision")},
    }
    if (
        not isinstance(build, dict)
        or build.get("schema") != "astrid.element-build/v1"
        or not isinstance(build.get('selectionDigest'), str)
        or not re.fullmatch(r'sha256:[0-9a-f]{64}', build['selectionDigest'])
        or not isinstance(build.get('elements'), list)
        or identity not in build.get("elements", [])
    ):
        raise ValueError("Selected source lacks a successful build receipt")
    if "pack.yaml" not in files:
        raise ValueError("Admitted snapshot pack manifest is missing")
    manifest = yaml.safe_load(files["pack.yaml"])
    if not isinstance(manifest, dict) or manifest.get("id") != owner:
        raise ValueError("Admitted snapshot pack owner mismatch")
    exact = []
    for declaration in manifest.get("rendering", {}).values():
        if declaration["type"] != "element":
            raise ValueError("Project draft pack admission supports rendering elements only")
        path = PurePosixPath(declaration["path"])
        # Admission uses the standard element manifest precedence.
        name = next(
            (
                path.parent / name
                for name in ("element.yaml", "element.yml", "element.json")
                if (path.parent / name).as_posix() in files
            ),
            None,
        )
        if name is None:
            raise ValueError("Selected pack element is unavailable")
        payload = (
            json.loads(files[name.as_posix()])
            if name.suffix == ".json"
            else yaml.safe_load(files[name.as_posix()])
        )
        definition = element_definition_from_payload(
            payload,
            root=path.parent,
            kind=payload["kind"],
            source=f"pack:{owner}",
            editable=True,
            priority=100,
            manifest_path=name,
            files=files,
        )
        if definition.id == reference["id"]:
            exact.append(_element_descriptor(definition, files=files))
    if len(exact) != 1 or any(exact[0].get(k) != reference[k] for k in required):
        raise ValueError("Admitted source does not match canonical revision")
    descriptor = exact[0]
    if 'compiledBindings' in build:
        bindings = build['compiledBindings']
        if not isinstance(bindings, list):
            raise ValueError('Invalid compiled binding receipt')
        matches = [b for b in bindings if isinstance(b, dict) and all(b.get(k) == v for k, v in identity.items())]
        component = descriptor['componentPath']
        compiled_path = _compiled_path(component)
        expected_compiled = {'path': compiled_path, 'sha256': hashlib.sha256(files[compiled_path]).hexdigest()} if compiled_path in files else None
        if (len(matches) != 1 or expected_compiled is None
                or matches[0] != {**identity, 'componentPath': component, 'compiled': expected_compiled}
                or admission.get('compiled') != expected_compiled):
            raise ValueError('Selected compiled binding does not match admitted source')
    descriptor["componentPath"] = (
        f"external/{owner}/{admission['snapshot_digest']}/{descriptor['componentPath']}"
    )
    return copy.deepcopy(descriptor)


def admitted_element_snapshot_descriptor(realm, project, reference, verified, snapshot):
    """Resolve only portable bytes returned by the scoped document_object port.

    Runtime owns capture/association authority; no caller-supplied object or
    local cache establishes that authority. This function is a pure validator.
    """
    if not isinstance(reference, dict) or not isinstance(verified, Mapping):
        raise ValueError('Missing selected project pack declaration')
    content = _project_pack_content(verified.get('document', {}))
    admitted = [a for a in content['admissions']
                if all(a.get(k) == reference.get(k) for k in ('id', 'kind', 'revision'))]
    if len(admitted) != 1:
        raise ValueError('Element owner/revision is not admitted for this project')
    expected = admitted[0].get('snapshot_object')
    if expected is None:
        raise ValueError('pack snapshot unavailable; re-admit pack')
    if not isinstance(snapshot, Mapping) or set(snapshot) != {*expected, 'bytes_base64'}:
        raise ValueError('Invalid verified pack snapshot object')
    descriptor = _snapshot_descriptor({key: snapshot[key] for key in expected})
    if descriptor != expected:
        raise ValueError('Pack snapshot object does not match selected admission')
    data = _decode_base64(snapshot['bytes_base64'])
    files, receipt = _snapshot_object_resources(descriptor, data)
    return admitted_element_descriptor(realm, project, reference, verified, files, receipt)


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
    files=_closure(root,entry,resources)
    digest=_closure_digest(files)
    if digest!=admitted[0]['snapshot_digest'] or files!=snapshot['files']:raise ValueError('Admitted element snapshot is missing or corrupt')
    verified={'pin':{'document_id':document_id,'version':row['version'],
                     'content_digest':_json_digest(row['content'])},
              'document':{**row,'project_id':project}}
    descriptor=admitted_element_descriptor(
        realm,project,reference,verified,{item['path']:(root/item['path']).read_bytes() for item in files},
        (root/'.build-receipt.json').read_bytes())
    if descriptor!=record['descriptor']:
        raise ValueError('Admitted source descriptor mismatch')
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
