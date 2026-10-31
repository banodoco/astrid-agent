"""Resumable, CPU-only preparation of a selected remote VibeComfy worker.

This module owns only staging evidence. It does not activate Runtime, qualify a
GPU, submit inference, or own the lifetime of the provider machine. The local
journal is keyed by the P1 claim operation; every resume re-observes remote
content and reruns checks in the configured child interpreter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol

from runtime_protocol import platform


class WorkerPreparationError(RuntimeError):
    """Selected preparation inputs or observations could not be verified."""


class PreparationJournalError(WorkerPreparationError):
    """The local operation journal is corrupt or bound to different inputs."""


class StagingEvidenceStaleError(WorkerPreparationError):
    """Previously prepared CPU evidence no longer matches the remote worker."""


_SCHEMA = "astrid.runpod.worker-preparation.v1"
_SHA256_PREFIX = "sha256:"
_CUSTOM_NODE_PIN_PACKS = {
    "h3_custom_node_commit": "ComfyUI-H3-Motion-Context-MultiRef",
    "videohelpersuite_commit": "ComfyUI-VideoHelperSuite",
}
_TREE_EXCLUDES = frozenset(
    {
        ".git",
        ".otto",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        "build",
        "dist",
    }
)
_REMOTE_PROBE_PROGRAM = r'''import hashlib,json,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1])
excluded=set(json.loads(sys.argv[2]))
for ancestor in reversed(root.parents):
 if ancestor==Path("/"): continue
 if ancestor.exists() and (ancestor.is_symlink() or ancestor.resolve(strict=True)!=ancestor): raise SystemExit("tree path traverses a symlink")
if not root.exists():
 print(json.dumps({"exists":False},separators=(",",":"))); raise SystemExit(0)
if root.is_symlink() or not root.is_dir(): raise SystemExit("tree root is not a regular directory")
rows=[]
for current,dirs,files in os.walk(root,followlinks=False):
 base=Path(current)
 for name in list(dirs):
  child=base/name
  if name in excluded:
   dirs.remove(name); continue
  if child.is_symlink(): raise SystemExit("tree contains symlink: "+str(child.relative_to(root)))
 for name in files:
  child=base/name
  if name in excluded or child.suffix in {".pyc",".pyo"}: continue
  if child.is_symlink() or not child.is_file(): raise SystemExit("tree contains unsafe file: "+str(child.relative_to(root)))
  digest=hashlib.sha256()
  with child.open("rb") as handle:
   for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
  rows.append({"path":child.relative_to(root).as_posix(),"size":child.stat().st_size,"sha256":"sha256:"+digest.hexdigest()})
rows.sort(key=lambda row:row["path"])
payload=json.dumps(rows,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
print(json.dumps({"exists":True,"file_count":len(rows),"tree_digest":"sha256:"+hashlib.sha256(payload).hexdigest()},sort_keys=True,separators=(",",":")))
'''

# The exact same verifier runs in the target's selected interpreter. It imports
# the staged Astrid/VibeComfy code and invokes the canonical existing model and
# workflow validators; no GPU action or worker receipt is produced.
_CHILD_CHECK_PROGRAM = r'''import base64,hashlib,importlib,importlib.util,json,os,subprocess,sys
from pathlib import Path
request=json.loads(base64.b64decode(sys.argv[1]))
cfg=request["effective_config"]
def require_absolute(name):
 value=cfg.get(name)
 if not isinstance(value,str) or not value.startswith("/"): raise RuntimeError(name+" must be an absolute selected path")
 path=Path(value)
 if os.path.normpath(value)!=value: raise RuntimeError(name+" contains a lexical alias")
 return path
def reject_shadow(path,label):
 path=Path(path)
 for component in (path,*path.parents):
  if component==Path("/"): continue
  if component.is_symlink() or (component.exists() and component.resolve(strict=True)!=component): raise RuntimeError(label+" traverses a symlink or shadow path: "+str(component))
expected_python=require_absolute("python_executable")
if Path(sys.executable).resolve(strict=True)!=expected_python.resolve(strict=True):
 raise RuntimeError("child interpreter differs from selected python_executable")
astrid_root=require_absolute("astrid_root").resolve(strict=True)
vibe_root=require_absolute("vibecomfy_root").resolve(strict=True)
comfy_root=require_absolute("comfy_root").resolve(strict=True)
model_root=require_absolute("model_root").resolve(strict=True)
output_root=require_absolute("output_root")
for path,label in ((astrid_root,"Astrid source"),(vibe_root,"VibeComfy source"),(comfy_root,"Comfy release"),(model_root,"model root")):
 reject_shadow(path,label)
if not astrid_root.is_dir() or not vibe_root.is_dir() or not comfy_root.is_dir(): raise RuntimeError("selected source or Comfy root is missing")
for ancestor in reversed(output_root.parents):
 if ancestor==Path("/"): continue
 if ancestor.exists() and (ancestor.is_symlink() or ancestor.resolve(strict=True)!=ancestor): raise RuntimeError("selected output root traverses a symlink")
if output_root.exists() and (output_root.is_symlink() or not output_root.is_dir() or output_root.resolve(strict=True)!=output_root): raise RuntimeError("selected output root is aliased or not a directory")
if not output_root.exists(): output_root.mkdir(parents=True,exist_ok=True)
for root in (astrid_root,vibe_root):
 text=str(root)
 sys.path[:]=[entry for entry in sys.path if entry!=text]
 sys.path.insert(0,text)
import astrid.core.generation.model_root as model_module
import astrid.packs.vibecomfy.invocation_preflight as preflight_module
import vibecomfy
def within(module,root,label):
 location=Path(module.__file__).resolve(strict=True)
 if not location.is_relative_to(root): raise RuntimeError(label+" imported from a different source tree: "+str(location))
 return str(location)
astrid_module=importlib.import_module("astrid")
astrid_import=within(astrid_module,astrid_root,"Astrid")
model_import=within(model_module,astrid_root,"model verifier")
preflight_import=within(preflight_module,astrid_root,"workflow preflight")
vibe_import=within(vibecomfy,vibe_root,"VibeComfy")
if Path(cfg["model_root"]).resolve(strict=True)!=model_root: raise RuntimeError("effective model root differs from selected model binding")
from astrid.core.generation.model_root import model_root_binding_from_profile
binding=model_root_binding_from_profile(request["readiness_profile"],verify_files=True)
if binding.path.resolve(strict=True)!=model_root: raise RuntimeError("child model verifier consumed a different model root")
contract=request["release_contract"]
candidate_path=Path(contract["candidate_namespace"])
reject_shadow(candidate_path,"release candidate")
candidate=candidate_path.resolve(strict=True)
if not candidate.is_dir() or not comfy_root.is_relative_to(candidate): raise RuntimeError("selected Comfy root is outside the manifest candidate namespace")
if Path(cfg["candidate_namespace"]).resolve(strict=True)!=candidate: raise RuntimeError("effective candidate namespace differs from the selected manifest")
if Path(cfg["python_executable"]).resolve(strict=True)!=candidate.joinpath(*contract["venv_path"].split("/")).joinpath("bin/python").resolve(strict=True): raise RuntimeError("selected interpreter is not the manifest runtime.venv interpreter")
launcher=candidate.joinpath(*contract["launcher_path"].split("/"))
if Path(cfg["launcher_path"]).resolve(strict=True)!=launcher.resolve(strict=True): raise RuntimeError("effective launcher differs from the selected manifest")
reject_shadow(launcher,"Comfy launcher")
if not launcher.is_file() or not os.access(launcher,os.X_OK): raise RuntimeError("manifest-selected Comfy launcher is missing or not executable")
syntax=subprocess.run(["sh","-n",str(launcher)],capture_output=True,text=True,check=False)
if syntax.returncode!=0: raise RuntimeError("manifest-selected Comfy launcher has invalid shell syntax")
def git_fact(root,label,expected):
 revision=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],capture_output=True,text=True,check=False)
 if revision.returncode!=0 or revision.stdout.strip()!=expected: raise RuntimeError(label+" checkout differs from the selected release manifest")
 dirty=subprocess.run(["git","-C",str(root),"status","--porcelain","--untracked-files=all"],capture_output=True,text=True,check=False)
 if dirty.returncode!=0 or dirty.stdout.strip(): raise RuntimeError(label+" checkout has uncommitted or untracked content")
 return revision.stdout.strip()
comfy_commit=git_fact(comfy_root,"ComfyUI",contract["comfyui_commit"])
lock_path=Path(contract["custom_nodes_lock_path"])
reject_shadow(lock_path,"custom-node lock")
if not lock_path.is_relative_to(candidate) or not lock_path.is_file(): raise RuntimeError("selected custom-node lock is missing or outside the release candidate")
lock=lock_path.resolve(strict=True)
lock_bytes=lock.read_bytes()
lock_digest="sha256:"+hashlib.sha256(lock_bytes).hexdigest()
if lock_digest!=contract["custom_nodes_lock_sha256"]: raise RuntimeError("selected custom-node lock differs from the release manifest")
import tomllib
lock_tables=tomllib.loads(lock_bytes.decode("utf-8")).get("nodepacks",{})
if not isinstance(lock_tables,dict): raise RuntimeError("selected custom-node lock has no nodepacks table")
def norm_pack(value): return "".join(character for character in str(value).casefold() if character.isalnum())
lock_rows=[]
for table_name,row in lock_tables.items():
 if isinstance(row,dict): lock_rows.append((str(table_name),row))
def lock_row_for_pack(pack_name):
 wanted=norm_pack(pack_name)
 matches=[]
 for table_name,row in lock_rows:
  aliases={norm_pack(table_name),norm_pack(row.get("name")),norm_pack(row.get("slug"))}
  aliases.discard("")
  if wanted in aliases: matches.append((table_name,row))
 if len(matches)!=1: raise RuntimeError("custom-node pack does not bind to one selected lock entry: "+str(pack_name))
 return matches[0]
def lock_row_commit(table_name,row):
 commits={str(row[key]) for key in ("commit","git_commit_sha") if row.get(key)}
 if len(commits)!=1: raise RuntimeError("selected custom-node lock entry has missing or conflicting commit pins: "+table_name)
 return next(iter(commits))
custom_node_facts={}
for name,row in contract["custom_node_roots"].items():
 pack_name=row["pack_name"]
 table_name,lock_row=lock_row_for_pack(pack_name)
 if lock_row_commit(table_name,lock_row)!=row["commit"]: raise RuntimeError("selected custom-node lock commit differs from the manifest pin: "+pack_name)
 root_path=Path(row["path"])
 reject_shadow(root_path,name)
 custom_nodes_root=comfy_root/"custom_nodes"
 if not root_path.is_relative_to(custom_nodes_root): raise RuntimeError("selected custom-node checkout is outside ComfyUI/custom_nodes: "+name)
 if norm_pack(root_path.name)!=norm_pack(pack_name): raise RuntimeError("selected custom-node checkout directory differs from its pinned pack: "+pack_name)
 root=root_path.resolve(strict=True)
 if not root.is_relative_to(custom_nodes_root): raise RuntimeError("selected custom-node checkout resolves outside ComfyUI/custom_nodes: "+name)
 custom_node_facts[name]={"commit":git_fact(root,name,row["commit"]),"lock_entry":table_name,"pack_name":pack_name,"path":str(root)}
if contract.get("python_version") and sys.version.split()[0]!=contract["python_version"]: raise RuntimeError("child Python version differs from the selected release manifest")
if contract.get("vibecomfy_version"):
 import tomllib
 project=tomllib.loads((vibe_root/"pyproject.toml").read_text(encoding="utf-8")).get("project",{})
 if project.get("name")!="vibecomfy" or project.get("version")!=contract["vibecomfy_version"]: raise RuntimeError("staged VibeComfy package version differs from the selected release manifest")
for distribution,expected in contract.get("package_versions",{}).items():
 try:
  from importlib.metadata import version
  observed=version(distribution)
 except Exception as exc: raise RuntimeError("selected dependency metadata is missing: "+distribution) from exc
 if observed!=expected: raise RuntimeError("selected dependency version drifted: "+distribution)
if contract.get("pip_check_required"):
 check=subprocess.run([sys.executable,"-X","utf8","-m","pip","check"],capture_output=True,text=True,check=False)
 if check.returncode!=0: raise RuntimeError("selected interpreter dependency closure failed pip check: "+(check.stdout+check.stderr).strip())
def tree_digest(root):
 rows=[]
 excluded=set(request["tree_excludes"])
 for current,dirs,files in os.walk(root,followlinks=False):
  base=Path(current)
  for name in list(dirs):
   child=base/name
   if name in excluded: dirs.remove(name); continue
   if child.is_symlink(): raise RuntimeError("child source contains symlink: "+str(child))
  for name in files:
   child=base/name
   if name in excluded or child.suffix in {".pyc",".pyo"}: continue
   if child.is_symlink() or not child.is_file(): raise RuntimeError("child source contains unsafe file: "+str(child))
   digest=hashlib.sha256()
   with child.open("rb") as handle:
    for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
   rows.append({"path":child.relative_to(root).as_posix(),"size":child.stat().st_size,"sha256":"sha256:"+digest.hexdigest()})
 rows.sort(key=lambda row:row["path"])
 payload=json.dumps(rows,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
 return "sha256:"+hashlib.sha256(payload).hexdigest(),len(rows)
tree_facts={}
for name in ("astrid_root","vibecomfy_root"):
 digest,count=tree_digest(Path(cfg[name]).resolve(strict=True))
 expected=request["expected_tree_digests"][name]
 if digest!=expected: raise RuntimeError(name+" remote bytes changed after staging")
 tree_facts[name]={"tree_digest":digest,"file_count":count}
comfy_tree_digest,comfy_file_count=tree_digest(comfy_root)
custom_node_tree_facts={}
for name,row in contract["custom_node_roots"].items():
 digest,count=tree_digest(Path(row["path"]).resolve(strict=True))
 custom_node_tree_facts[name]={"tree_digest":digest,"file_count":count}
engine=str(cfg.get("engine_mode") or "")
if engine not in {"checkout_server","pip_embedded"}: raise RuntimeError("unsupported Comfy engine mode")
if engine=="checkout_server" and not (comfy_root/"main.py").is_file(): raise RuntimeError("selected Comfy checkout has no main.py")
if engine=="pip_embedded" and not (comfy_root/"comfy/client/embedded_comfy_client.py").is_file(): raise RuntimeError("selected embedded Comfy release is incomplete")
workflow=Path(request["workflow_path"]).resolve(strict=True)
workflow_bundle=request.get("workflow_bundle")
if workflow_bundle is None:
 if not any(workflow.is_relative_to(root) for root in (astrid_root,vibe_root)):
  raise RuntimeError("selected workflow is outside the staged source roots")
else:
 if not isinstance(workflow_bundle,dict) or set(workflow_bundle)!={"root","members"}:
  raise RuntimeError("selected workflow bundle contract is malformed")
 bundle_root=Path(workflow_bundle["root"]).resolve(strict=True)
 if workflow != bundle_root/"workflow.py": raise RuntimeError("selected workflow differs from the staged bundle")
 members=workflow_bundle["members"]
 if not isinstance(members,dict) or set(members)!={"workflow.py","workflow.vibe.json","source.json"}:
  raise RuntimeError("selected workflow bundle has an incomplete member set")
 for name,expected_digest in members.items():
  member=bundle_root/name
  if member.is_symlink() or not member.is_file(): raise RuntimeError("selected workflow bundle has an unsafe member: "+name)
  digest="sha256:"+hashlib.sha256(member.read_bytes()).hexdigest()
  if digest!=expected_digest: raise RuntimeError("selected workflow bundle member changed after staging: "+name)
from astrid.packs.vibecomfy.invocation_preflight import preflight_invocation
compiled=preflight_invocation(workflow,run_inputs=request["workflow_inputs"],phase="preparation")
from astrid.packs.vibecomfy.production_engine import load_workflow_path
import tempfile
with tempfile.TemporaryDirectory(prefix="astrid-workflow-requirements-") as scratch:
 loaded=load_workflow_path(workflow,scratch)
 requirements=getattr(getattr(loaded.resolved,"workflow",None),"requirements",None)
 required=list(getattr(requirements,"custom_nodes",[]) or [])
if any(not isinstance(name,str) or not name.strip() for name in required): raise RuntimeError("selected workflow custom-node requirements are malformed")
for pack_name in required:
 table_name,lock_row=lock_row_for_pack(pack_name)
 bindings=[fact for fact in custom_node_facts.values() if norm_pack(fact["pack_name"])==norm_pack(pack_name)]
 if len(bindings)!=1: raise RuntimeError("selected workflow requires a custom-node pack without exactly one manifest-pinned checkout/root: "+pack_name)
 if bindings[0]["lock_entry"]!=table_name or lock_row_commit(table_name,lock_row)!=bindings[0]["commit"]: raise RuntimeError("selected workflow custom-node pack is not bound to its pinned lock entry and observed checkout: "+pack_name)
print(json.dumps({"schema_version":1,"status":"cpu_ready","gpu_qualified":False,"capability_id":request["capability_id"],"python_executable":str(Path(sys.executable).resolve()),"python_version":sys.version.split()[0],"imports":{"astrid":astrid_import,"model_root_verifier":model_import,"vibecomfy_preflight":preflight_import,"vibecomfy":vibe_import},"effective_paths":{"astrid_root":str(astrid_root),"vibecomfy_root":str(vibe_root),"comfy_root":str(comfy_root),"model_root":str(binding.path),"output_root":str(output_root.resolve(strict=True)),"candidate_namespace":str(candidate),"launcher_path":str(launcher)},"release_facts":{"release_id":cfg["comfy_release_id"],"manifest_state":contract["manifest_state"],"candidate_namespace":str(candidate),"comfyui_commit":comfy_commit,"comfy_tree_digest":comfy_tree_digest,"comfy_file_count":comfy_file_count,"launcher_path":str(launcher),"launcher_sha256":"sha256:"+hashlib.sha256(launcher.read_bytes()).hexdigest(),"custom_nodes_lock_sha256":lock_digest,"custom_node_commits":custom_node_facts,"custom_node_trees":custom_node_tree_facts,"required_custom_nodes":sorted(required)},"tree_facts":tree_facts,"model_root":binding.as_dict(),"workflow_preflight":compiled},sort_keys=True,separators=(",",":")))
'''


class WorkerPreparationTransport(Protocol):
    """Small command/SFTP boundary; production uses runpod-lifecycle Pod."""

    async def probe_tree(self, remote_path: str) -> Mapping[str, Any]: ...

    async def upload_path(self, local_path: Path, remote_path: str) -> None: ...

    async def remove_owned_tree(self, remote_path: str, owned_root: str) -> None: ...

    async def child_check(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


_ENDPOINT_FILE_PROGRAM = r'''import base64,hashlib,json,os,stat,sys,time
operation,path,budget,temporary,expected,previous=sys.argv[1:]
deadline=time.monotonic()+float(budget)
limit=8*1024*1024
parts=path.split('/')[1:]
if not path.startswith('/') or any(p in ('','.','..') for p in parts) or '\\' in path: raise ValueError()
directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
try:
 for part in parts[:-1]:
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=directory)
  os.close(directory); directory=child
 def read(name):
  fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=directory)
  try:
   before=os.fstat(fd)
   if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or before.st_mode&0o022 or before.st_size>limit: raise ValueError()
   data=bytearray()
   while True:
    if time.monotonic()>=deadline: raise TimeoutError()
    block=os.read(fd,min(65536,limit+1-len(data)))
    if not block: break
    data.extend(block)
    if len(data)>limit: raise ValueError()
   after=os.fstat(fd)
   if len(data)!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ValueError()
   return bytes(data)
  finally: os.close(fd)
 if operation=='read':
  data=read(parts[-1])
  result={'data':base64.b64encode(data).decode(),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
 else:
  info=os.fstat(directory)
  if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700 or not __import__('re').fullmatch(r'\.engine-readiness-[0-9a-f]{32}',temporary): raise ValueError()
  if operation=='reserve':
   fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
   os.close(fd); result={'reserved':True}
  elif operation=='commit':
   if hashlib.sha256(read(parts[-1])).hexdigest()!=previous or hashlib.sha256(read(temporary)).hexdigest()!=expected: raise ValueError()
   os.replace(temporary,parts[-1],src_dir_fd=directory,dst_dir_fd=directory)
   os.fsync(directory); result={'committed':True}
  else: raise ValueError()
 print(json.dumps(result,separators=(',',':')))
finally: os.close(directory)
'''


class RunPodPreparationTransport:
    """Adapter over ``Pod.exec_ssh`` and the existing ``Pod.upload_path``."""

    def __init__(self, pod: Any, *, timeout_seconds: int = 3600) -> None:
        self.pod = pod
        self.timeout_seconds = timeout_seconds

    async def _run_json_program(
        self, program: str, args: list[str], *, python: str = "python3",
        timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        payload = base64.b64encode(
            json.dumps(args, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode("ascii")
        wrapper = (
            "import base64,json,sys; program=base64.b64decode(sys.argv[1]); "
            "args=json.loads(base64.b64decode(sys.argv[2])); "
            "sys.argv=['<astrid-preparation>',*args]; "
            "exec(compile(program,'<astrid-preparation>','exec'))"
        )
        encoded_program = base64.b64encode(program.encode()).decode("ascii")
        command = (
            f"{shlex.quote(python)} -X utf8 -c {shlex.quote(wrapper)} "
            f"{shlex.quote(encoded_program)} {shlex.quote(payload)}"
        )
        code, stdout, stderr = await self.pod.exec_ssh(command, timeout=timeout_seconds or self.timeout_seconds)
        if code != 0:
            raise WorkerPreparationError(
                f"remote preparation command failed (exit {code}): {(stderr or stdout).strip()}"
            )
        try:
            lines = [line for line in stdout.splitlines() if line.strip()]
            value = json.loads(lines[-1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkerPreparationError("remote preparation command returned no valid JSON evidence") from exc
        if not isinstance(value, dict):
            raise WorkerPreparationError("remote preparation command returned malformed evidence")
        return value

    def select_endpoint_files(self, *, profile_path: str, runtime_root: str, profile_hash: str) -> None:
        """Select only this original deployment's profile and installed evidence."""
        profile = _remote_absolute(profile_path, label="endpoint readiness")
        runtime = _remote_absolute(runtime_root, label="installed runtime")
        if (not runtime.endswith("/runtime")
                or PurePosixPath(profile).name in {"engine-bearer.token", "executor.token"}):
            raise WorkerPreparationError("trusted startup endpoint readiness selection is unavailable")
        root = str(PurePosixPath(runtime).parent)
        registry = runtime + "/out/sessions/vibecomfy-h3/"
        selected = frozenset({profile, root + "/.astrid-engine/install.json", *(
            registry + name for name in (
                "config.json", "launch.json", "pid", "comfy_pid", "comfy_process_start_identity",
                "url", "source_revision", "source_content_digest", "daemon.log",
            )
        )})
        previous = getattr(self, "_endpoint_files", selected)
        if previous != selected:
            raise WorkerPreparationError("endpoint file selection changed")
        self._endpoint_files = selected
        self._endpoint_profile = profile
        self._endpoint_profile_hash = profile_hash

    async def _endpoint_file_operation(self, action: str, path: str, *, deadline: float,
                                       staging: str = "", sha256: str = "", previous: str = ""):
        import time
        from astrid.core.generation.engine_contracts import DeadlineExceeded

        if path not in getattr(self, "_endpoint_files", ()):
            raise WorkerPreparationError("endpoint evidence file was not selected by the original owner")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("endpoint file deadline exceeded")
        try:
            result = await self._run_json_program(
                _ENDPOINT_FILE_PROGRAM, [action, path, str(min(20, remaining)), staging, sha256, previous],
                timeout_seconds=min(20, remaining),
            )
        except Exception:
            raise WorkerPreparationError(
                "trusted endpoint file handoff unavailable; observation-only commands/uploads remain denied"
            ) from None
        if time.monotonic() >= deadline:
            raise DeadlineExceeded("endpoint file deadline exceeded")
        return result

    async def read_endpoint_file(self, path: str, *, deadline: float) -> bytes:
        result = await self._endpoint_file_operation("read", path, deadline=deadline)
        try:
            raw = base64.b64decode(result["data"], validate=True)
            if (len(raw) > 8 * 1024 * 1024 or type(result["size"]) is not int
                    or len(raw) != result["size"] or hashlib.sha256(raw).hexdigest() != result["sha256"]):
                raise ValueError()
            return raw
        except (KeyError, TypeError, ValueError):
            raise WorkerPreparationError("endpoint file readback is invalid") from None

    async def write_endpoint_profile(self, path: str, profile: Mapping[str, Any], *, deadline: float) -> None:
        import secrets
        import time

        if path != getattr(self, "_endpoint_profile", None):
            raise WorkerPreparationError("endpoint profile was not selected by the original owner")
        raw = json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode()
        if len(raw) > 8 * 1024 * 1024:
            raise WorkerPreparationError("endpoint profile exceeds file limit")
        # Reserve/commit use target fd-relative opens. Contents travel only in
        # the existing upload body, never in a remote command's argv.
        before = await self.read_endpoint_file(path, deadline=deadline)
        if "sha256:" + hashlib.sha256(before).hexdigest() != self._endpoint_profile_hash:
            raise WorkerPreparationError("endpoint profile changed before finalization")
        temporary = ".engine-readiness-" + secrets.token_hex(16)
        await self._endpoint_file_operation("reserve", path, deadline=deadline, staging=temporary)
        with tempfile.TemporaryDirectory(prefix="astrid-endpoint-") as directory:
            local = Path(directory) / "profile"
            fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            import asyncio
            try:
                await asyncio.wait_for(self.pod.put_file(
                    local, str(PurePosixPath(path).parent / temporary), reserved=True,
                ), timeout=max(0.001, deadline - time.monotonic()))
            except Exception:
                raise WorkerPreparationError("trusted endpoint profile body transfer is unavailable") from None
        await self._endpoint_file_operation(
            "commit", path, deadline=deadline, staging=temporary,
            sha256=hashlib.sha256(raw).hexdigest(), previous=hashlib.sha256(before).hexdigest(),
        )
        if await self.read_endpoint_file(path, deadline=deadline) != raw:
            raise WorkerPreparationError("endpoint profile readback differs")


    async def probe_tree(self, remote_path: str) -> Mapping[str, Any]:
        return await self._run_json_program(
            _REMOTE_PROBE_PROGRAM,
            [remote_path, json.dumps(sorted(_TREE_EXCLUDES))],
        )

    async def upload_path(self, local_path: Path, remote_path: str) -> None:
        parent = str(PurePosixPath(remote_path).parent)
        code, stdout, stderr = await self.pod.exec_ssh(
            f"mkdir -p -- {shlex.quote(parent)}", timeout=self.timeout_seconds
        )
        if code != 0:
            raise WorkerPreparationError(
                f"cannot create the operation-owned stage parent: {(stderr or stdout).strip()}"
            )
        await self.pod.upload_path(local_path, remote_path, exclude=set(_TREE_EXCLUDES))

    async def remove_owned_tree(self, remote_path: str, owned_root: str) -> None:
        safe = _remote_absolute(remote_path, label="owned path")
        owner = _remote_absolute(owned_root, label="operation-owned root")
        if not safe.startswith(owner.rstrip("/") + "/"):
            raise WorkerPreparationError("refusing to remove a path outside this operation's owned root")
        guard = r'''import json,shutil,sys
from pathlib import Path
root=Path(sys.argv[1]); target=Path(sys.argv[2])
if not target.is_relative_to(root) or target==root: raise SystemExit("target is outside operation root")
for path in (root, *reversed(target.parents)):
 if path==Path("/"): continue
 if path.exists() and (path.is_symlink() or path.resolve(strict=True)!=path): raise SystemExit("operation path traverses a symlink")
if target.is_symlink(): target.unlink()
elif target.exists(): shutil.rmtree(target)
print(json.dumps({"removed":True},separators=(",",":")))
'''
        result = await self._run_json_program(guard, [owner, safe])
        if result.get("removed") is not True:
            raise WorkerPreparationError("remote owned path cleanup was not confirmed")

    async def child_check(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        python = str(request["effective_config"]["python_executable"])
        return await self._run_json_program(
            _CHILD_CHECK_PROGRAM,
            [base64.b64encode(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).decode()],
            python=python,
        )


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _SHA256_PREFIX + hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return _SHA256_PREFIX + digest.hexdigest()


def _normalize_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith(_SHA256_PREFIX):
        raise WorkerPreparationError(f"{label} must be a sha256 digest")
    suffix = value[len(_SHA256_PREFIX) :]
    if any(character not in "0123456789abcdef" for character in suffix):
        raise WorkerPreparationError(f"{label} must be a lowercase sha256 digest")
    return value


def _secret_free(value: Any, *, path: str = "input") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key).casefold().replace("-", "_")
            if any(marker in label for marker in ("api_key", "token", "password", "secret", "private_key")):
                raise WorkerPreparationError(f"{path}.{key} is secret-bearing and cannot enter preparation inputs")
            _secret_free(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _secret_free(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if value.startswith(("rpa_", "sk-")) or any(
            marker in lowered for marker in ("-----begin private key-----", "runpod_api_key=")
        ):
            raise WorkerPreparationError(f"{path} appears to contain a secret value")


def _safe_token(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise WorkerPreparationError(f"{label} must be a non-empty bounded string")
    return value.strip()


def _remote_absolute(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise WorkerPreparationError(f"{label} must be an absolute remote path")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) != value.rstrip("/"):
        raise WorkerPreparationError(f"{label} must not contain lexical aliases")
    return str(path)


def _tree_inventory(root: Path) -> tuple[str, int]:
    root = root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise WorkerPreparationError(f"source tree is not a regular directory: {root}")
    root = root.resolve(strict=True)
    rows: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(directories):
            child = base / name
            if name in _TREE_EXCLUDES:
                directories.remove(name)
                continue
            if child.is_symlink():
                raise WorkerPreparationError(f"source tree contains a symlink: {child.relative_to(root)}")
        for name in files:
            child = base / name
            if name in _TREE_EXCLUDES or child.suffix in {".pyc", ".pyo"}:
                continue
            if child.is_symlink() or not child.is_file():
                raise WorkerPreparationError(f"source tree contains an unsafe file: {child.relative_to(root)}")
            rows.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "size": child.stat().st_size,
                    "sha256": _file_digest(child),
                }
            )
    rows.sort(key=lambda row: row["path"])
    return _canonical_digest(rows), len(rows)


def _atomic_private_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            platform.owner_only(stream.fileno())
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        platform.durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PreparationJournalError("preparation journal is not a regular file; refusing to reuse")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationJournalError(f"preparation journal is corrupt; refusing to reuse: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        raise PreparationJournalError("preparation journal has an unsupported schema")
    return value


def _locked_journal(path: Path):
    class Lock:
        def __enter__(self):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                platform.lock_file(self.fd, blocking=False)
            except BlockingIOError as exc:
                os.close(self.fd)
                raise PreparationJournalError("another coordinator owns this preparation journal") from exc
            return self

        def __exit__(self, *_exc):
            try:
                platform.unlock_file(self.fd)
            finally:
                os.close(self.fd)

    return Lock()


def _claim_identity(handle_path: Path, provider_account_ref: str | None = None) -> dict[str, Any]:
    try:
        handle = json.loads(handle_path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerPreparationError(f"cannot read the exact P1 claim handle: {exc}") from exc
    if not isinstance(handle, dict) or handle.get("state") != "claimed":
        raise WorkerPreparationError("preparation requires an exact P1 handle in claimed state")
    operation_id = _safe_token(handle.get("operation_id"), label="P1 operation_id")
    pod_id = _safe_token(handle.get("pod_id"), label="P1 pod_id")
    volume_id = _safe_token(handle.get("network_volume_id"), label="P1 network_volume_id")
    volume_name = _safe_token(handle.get("network_volume_name"), label="P1 network_volume_name")
    volume_size = handle.get("network_volume_size_gb")
    if type(volume_size) is not int or volume_size <= 0:
        raise WorkerPreparationError("P1 handle has no verified positive network-volume size")
    mount = _remote_absolute(handle.get("volume_mount_path"), label="P1 volume_mount_path")
    datacenter = _safe_token(
        handle.get("network_volume_datacenter_id"), label="P1 network_volume_datacenter_id"
    )
    if handle.get("provider_account_ref") is None:
        raise WorkerPreparationError(
            "P1 claim handle has no stable provider_account_ref; api_key_ref is not account identity"
        )
    account_ref = _safe_token(handle["provider_account_ref"], label="P1 provider_account_ref")
    if provider_account_ref is None or _safe_token(
        provider_account_ref, label="provider_account_ref"
    ) != account_ref:
        raise WorkerPreparationError("caller provider account reference differs from exact P1 custody")
    return {
        "provider": "runpod",
        "provider_account_ref": account_ref,
        "operation_id": operation_id,
        "pod_id": pod_id,
        "volume_id": volume_id,
        "volume_name": volume_name,
        "volume_size_gb": volume_size,
        "volume_datacenter_id": datacenter,
        "volume_mount_path": mount,
    }


def _remote_owned_root(identity: Mapping[str, Any]) -> str:
    op_digest = hashlib.sha256(str(identity["operation_id"]).encode()).hexdigest()[:24]
    return str(PurePosixPath(str(identity["volume_mount_path"])) / ".astrid" / "prepared-workers" / op_digest)


def _map_workflow(local: Path, source_roots: Mapping[str, tuple[Path, str]]) -> str:
    source = local.expanduser().resolve(strict=True)
    matches: list[tuple[int, str]] = []
    for _name, (local_root, remote_root) in source_roots.items():
        root = local_root.expanduser().resolve(strict=True)
        if source.is_relative_to(root):
            matches.append((len(root.parts), str(PurePosixPath(remote_root) / source.relative_to(root).as_posix())))
    if not matches:
        raise WorkerPreparationError("selected workflow must be inside a staged source tree")
    return max(matches)[1]


def _git_head(root: Path, *, label: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    if len(revision) not in {40, 64} or any(character not in "0123456789abcdef" for character in revision):
        raise WorkerPreparationError(f"selected {label} checkout returned an invalid Git revision")
    return revision


def _git_clean_head(root: Path, *, label: str) -> str:
    revision = _git_head(root, label=label)
    if revision is None:
        raise WorkerPreparationError(f"selected {label} source is not a Git checkout")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise WorkerPreparationError(f"selected {label} source has uncommitted or untracked content")
    return revision


def _validate_release_manifest(
    path: Path,
    expected_digest: str,
    release_id: str,
    *,
    target_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise WorkerPreparationError("selected release manifest must be a regular file")
    source = source.resolve(strict=True)
    digest = _normalize_digest(expected_digest, label="release manifest digest")
    actual = _file_digest(source)
    if actual != digest:
        raise WorkerPreparationError("selected release manifest bytes do not match the supplied digest")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerPreparationError("selected release manifest is unreadable JSON") from exc
    if not isinstance(value, dict) or value.get("release_id") != release_id:
        raise WorkerPreparationError("selected release manifest does not name the explicit release_id")
    volume = value.get("volume")
    if not isinstance(volume, Mapping):
        raise WorkerPreparationError("selected release manifest must pin its network volume")
    if (
        volume.get("id") != target_identity["volume_id"]
        or volume.get("size_gb") != target_identity["volume_size_gb"]
        or volume.get("mount") != target_identity["volume_mount_path"]
        or volume.get("datacenter") != target_identity["volume_datacenter_id"]
        or (volume.get("name") is not None and volume.get("name") != target_identity["volume_name"])
    ):
        raise WorkerPreparationError("selected release volume does not match exact P1 claim custody")
    candidate_namespace = _remote_absolute(
        value.get("candidate_namespace"), label="release candidate_namespace"
    )
    mount = str(target_identity["volume_mount_path"]).rstrip("/")
    if not candidate_namespace.startswith(mount + "/"):
        raise WorkerPreparationError("selected release candidate is outside the exact P1 network volume")
    value["candidate_namespace"] = candidate_namespace
    return {"path": str(source), "digest": digest, "release_id": release_id}, value


def _safe_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkerPreparationError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value in {".", ""}:
        raise WorkerPreparationError(f"{label} must be a safe relative POSIX path")
    return value


def _release_contract(
    manifest: Mapping[str, Any],
    *,
    model_binding: Mapping[str, Any],
    vibecomfy_source: Path,
    comfy_root: str,
    custom_nodes_lock_path: str,
    custom_node_roots: Mapping[str, str],
) -> dict[str, Any]:
    platform = manifest.get("platform")
    runtime = manifest.get("runtime")
    manifest_state = manifest.get("state")
    if not isinstance(manifest_state, str) or not manifest_state.strip():
        raise WorkerPreparationError("selected release manifest must declare its state")
    platform = platform if isinstance(platform, Mapping) else {}
    runtime = runtime if isinstance(runtime, Mapping) else {}
    expected_vibe_revision = runtime.get("vibecomfy_source_commit")
    expected_comfy_revision = runtime.get("comfyui_commit")
    if not isinstance(expected_vibe_revision, str) or not expected_vibe_revision.strip():
        raise WorkerPreparationError("selected release manifest must pin runtime.vibecomfy_source_commit")
    if not isinstance(expected_comfy_revision, str) or not expected_comfy_revision.strip():
        raise WorkerPreparationError("selected release manifest must pin runtime.comfyui_commit")
    observed_vibe_revision = _git_clean_head(vibecomfy_source, label="VibeComfy")
    if observed_vibe_revision != expected_vibe_revision:
        raise WorkerPreparationError("selected VibeComfy source does not match the manifest commit")
    candidate = _remote_absolute(manifest["candidate_namespace"], label="release candidate_namespace")
    venv_path = _safe_relative_path(runtime.get("venv"), label="runtime.venv")
    launcher_path = _safe_relative_path(runtime.get("launcher"), label="runtime.launcher")
    selected_comfy_root = _remote_absolute(comfy_root, label="selected Comfy root")
    selected_lock = _remote_absolute(custom_nodes_lock_path, label="custom_nodes_lock_path")
    for label, path in (("Comfy root", selected_comfy_root), ("custom-node lock", selected_lock)):
        if not path.startswith(candidate.rstrip("/") + "/"):
            raise WorkerPreparationError(f"selected {label} is outside the manifest candidate namespace")
    lock_digest = runtime.get("custom_nodes_lock_sha256")
    if not isinstance(lock_digest, str) or len(lock_digest) != 64 or any(c not in "0123456789abcdef" for c in lock_digest):
        raise WorkerPreparationError("selected release manifest must pin runtime.custom_nodes_lock_sha256")
    node_roots: dict[str, dict[str, str]] = {}
    for field, pack_name in _CUSTOM_NODE_PIN_PACKS.items():
        commit = runtime.get(field)
        if commit is None:
            continue
        if not isinstance(commit, str) or len(commit) not in {40, 64} or any(c not in "0123456789abcdef" for c in commit):
            raise WorkerPreparationError(f"selected release runtime.{field} pin is invalid")
        raw_path = custom_node_roots.get(field)
        if raw_path is None:
            raise WorkerPreparationError(f"selected custom-node checkout path is required for runtime.{field}")
        path = _remote_absolute(raw_path, label=f"custom_node_roots.{field}")
        if not path.startswith(candidate.rstrip("/") + "/"):
            raise WorkerPreparationError(f"custom-node checkout {field} is outside the manifest candidate namespace")
        node_roots[field] = {"path": path, "commit": commit, "pack_name": pack_name}
    if set(custom_node_roots) != set(node_roots):
        raise WorkerPreparationError("custom_node_roots must name exactly the selected release custom-node commit pins")
    expected_models = manifest.get("models")
    if not isinstance(expected_models, list):
        raise WorkerPreparationError("selected release manifest must contain an explicit models inventory (empty is allowed)")
    normalized_models = []
    for row in expected_models:
        if not isinstance(row, Mapping):
            raise WorkerPreparationError("selected release manifest models inventory is malformed")
        raw_path = row.get("path")
        if not isinstance(raw_path, str) or not raw_path or PurePosixPath(raw_path).is_absolute() or ".." in PurePosixPath(raw_path).parts:
            raise WorkerPreparationError("selected release manifest model path is unsafe")
        path = PurePosixPath(raw_path)
        size = row.get("bytes")
        digest = row.get("sha256")
        if type(size) is not int or size < 0:
            raise WorkerPreparationError("selected release manifest model size is invalid")
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise WorkerPreparationError("selected release manifest model sha256 is invalid")
        normalized_models.append(
            {
                "subdir": "" if str(path.parent) == "." else str(path.parent),
                "name": path.name,
                "size": size,
                "sha256": _SHA256_PREFIX + digest,
            }
        )
    normalized_models.sort(key=lambda row: (row["subdir"], row["name"]))
    bound_models = [
        {
            "subdir": entry.get("subdir", ""),
            "name": entry.get("name"),
            "size": entry.get("size"),
            "sha256": entry.get("sha256"),
        }
        for entry in model_binding.get("inventory", [])
        if isinstance(entry, Mapping)
    ]
    bound_models.sort(key=lambda row: (row["subdir"], row["name"]))
    if normalized_models != bound_models:
        raise WorkerPreparationError("readiness model-root inventory differs from the selected release manifest")
    versions: dict[str, str] = {}
    for field, distribution in (
        ("torch", "torch"),
        ("torchvision", "torchvision"),
        ("torchaudio", "torchaudio"),
        ("comfy_aimdo", "comfy-aimdo"),
        ("comfy_kitchen", "comfy-kitchen"),
    ):
        version = runtime.get(field)
        if version is not None:
            if not isinstance(version, str) or not version.strip():
                raise WorkerPreparationError(f"selected release runtime.{field} pin is invalid")
            versions[distribution] = version.strip()
    return {
        "python_version": platform.get("python"),
        "vibecomfy_version": runtime.get("vibecomfy"),
        "package_versions": versions,
        "comfyui_commit": expected_comfy_revision,
        "vibecomfy_source_commit": expected_vibe_revision,
        "manifest_state": manifest_state.strip(),
        "candidate_namespace": candidate,
        "venv_path": venv_path,
        "launcher_path": launcher_path,
        "custom_nodes_lock_path": selected_lock,
        "custom_nodes_lock_sha256": _SHA256_PREFIX + lock_digest,
        "custom_node_roots": node_roots,
        "pip_check_required": isinstance(runtime.get("dependency_check"), str)
        and "pip check" in runtime["dependency_check"].casefold(),
        "model_inventory_digest": _canonical_digest(normalized_models),
    }


def _check_selected_profile(readiness_profile: Mapping[str, Any], config: Mapping[str, Any]) -> Mapping[str, Any]:
    launch = readiness_profile.get("launch")
    if not isinstance(launch, Mapping):
        raise WorkerPreparationError("selected readiness profile has no launch configuration")
    binding = launch.get("model_root")
    if not isinstance(binding, Mapping):
        raise WorkerPreparationError("selected readiness profile has no explicit model-root binding")
    selected_model = binding.get("path")
    if selected_model != config.get("model_root"):
        raise WorkerPreparationError("effective model root differs from the selected readiness profile")
    profile_output = launch.get("output_root")
    if not isinstance(profile_output, str) or profile_output != config.get("output_root"):
        raise WorkerPreparationError("effective output root differs from the selected readiness profile")
    return binding


def _build_plan(
    *,
    claim_handle_path: Path,
    provider_account_ref: str,
    release_manifest_path: Path,
    release_manifest_sha256: str,
    release_id: str,
    resolved_compute_profile: Mapping[str, Any],
    readiness_profile: Mapping[str, Any],
    astrid_source: Path,
    vibecomfy_source: Path,
    comfy_root: str,
    custom_nodes_lock_path: str,
    custom_node_roots: Mapping[str, str],
    python_executable: str,
    engine_mode: str,
    workflow_path: Path,
    workflow_bundle_path: Path | None,
    workflow_inputs: Mapping[str, Any],
    capability_id: str,
) -> dict[str, Any]:
    identity = _claim_identity(claim_handle_path, provider_account_ref)
    release_info, manifest = _validate_release_manifest(
        release_manifest_path,
        release_manifest_sha256,
        release_id,
        target_identity=identity,
    )
    _secret_free(resolved_compute_profile, path="resolved_compute_profile")
    _secret_free(readiness_profile, path="readiness_profile")
    _secret_free(workflow_inputs, path="workflow_inputs")
    capability = _safe_token(capability_id, label="capability_id")
    python = _remote_absolute(python_executable, label="python_executable")
    if engine_mode not in {"checkout_server", "pip_embedded"}:
        raise WorkerPreparationError("engine_mode must be checkout_server or pip_embedded")
    profile = json.loads(json.dumps(resolved_compute_profile, sort_keys=True, ensure_ascii=False))
    readiness = json.loads(json.dumps(readiness_profile, sort_keys=True, ensure_ascii=False))
    source_paths = {
        "astrid": astrid_source.expanduser().resolve(strict=True),
        "vibecomfy": vibecomfy_source.expanduser().resolve(strict=True),
    }
    remote_root = _remote_owned_root(identity)
    source_remote = {
        "astrid": str(PurePosixPath(remote_root) / "source" / "astrid"),
        "vibecomfy": str(PurePosixPath(remote_root) / "source" / "vibecomfy"),
    }
    comfy_remote = _remote_absolute(comfy_root, label="selected Comfy root")
    output_root = _remote_absolute(readiness["launch"]["output_root"], label="output_root")
    model_binding = _check_selected_profile(readiness, {
        "model_root": readiness["launch"]["model_root"]["path"],
        "output_root": output_root,
    })
    release_contract = _release_contract(
        manifest,
        model_binding=model_binding,
        vibecomfy_source=source_paths["vibecomfy"],
        comfy_root=comfy_remote,
        custom_nodes_lock_path=custom_nodes_lock_path,
        custom_node_roots=custom_node_roots,
    )
    expected_python = str(
        PurePosixPath(release_contract["candidate_namespace"])
        / release_contract["venv_path"]
        / "bin"
        / "python"
    )
    if python != expected_python:
        raise WorkerPreparationError("python_executable differs from the selected manifest runtime.venv")
    expected_launcher = str(
        PurePosixPath(release_contract["candidate_namespace"]) / release_contract["launcher_path"]
    )
    workflow_local = workflow_path.expanduser().resolve(strict=True)
    bundle_plan = None
    if workflow_bundle_path is None:
        source_mapping = {
            name: (source_paths[name], source_remote[name]) for name in source_paths
        }
        remote_workflow = _map_workflow(workflow_local, source_mapping)
    else:
        bundle_root = workflow_bundle_path.expanduser()
        if bundle_root.is_symlink() or not bundle_root.is_dir():
            raise WorkerPreparationError("selected workflow bundle must be a regular operation-owned directory")
        bundle_root = bundle_root.resolve(strict=True)
        expected_members = {"workflow.py", "workflow.vibe.json", "source.json"}
        actual_members = {item.name for item in bundle_root.iterdir()}
        if actual_members != expected_members or workflow_local != bundle_root / "workflow.py":
            raise WorkerPreparationError("selected workflow bundle must contain the exact canonical H3 members")
        members: dict[str, str] = {}
        for name in sorted(expected_members):
            member = bundle_root / name
            if member.is_symlink() or not member.is_file():
                raise WorkerPreparationError(f"selected workflow bundle member is unsafe: {name}")
            members[name] = _file_digest(member)
        bundle_digest, bundle_count = _tree_inventory(bundle_root)
        workflow_remote_root = str(PurePosixPath(remote_root) / "workflow")
        remote_workflow = str(PurePosixPath(workflow_remote_root) / "workflow.py")
        bundle_plan = {
            "local_path": str(bundle_root),
            "remote_root": workflow_remote_root,
            "tree_digest": bundle_digest,
            "file_count": bundle_count,
            "members": members,
        }
    config = {
        "python_executable": python,
        "astrid_root": source_remote["astrid"],
        "vibecomfy_root": source_remote["vibecomfy"],
        "comfy_root": comfy_remote,
        "model_root": str(model_binding["path"]),
        "output_root": output_root,
        "engine_mode": engine_mode,
        "comfy_release_id": release_id,
        "candidate_namespace": release_contract["candidate_namespace"],
        "launcher_path": expected_launcher,
    }
    # The old selected paths, if present, are authority. A new plan may not
    # silently redirect a worker away from the selected readiness profile.
    for field in ("python_executable", "astrid_root", "vibecomfy_root", "comfy_root", "output_root", "launcher_path"):
        selected = readiness["launch"].get(field)
        if selected is not None and selected != config[field]:
            raise WorkerPreparationError(f"effective {field} differs from the selected readiness profile")
    source_facts: dict[str, Any] = {}
    for name, local in source_paths.items():
        digest, count = _tree_inventory(local)
        source_facts[name] = {
            "local_path": str(local),
            "remote_path": source_remote[name],
            "tree_digest": digest,
            "file_count": count,
        }
    plan = {
        "schema": _SCHEMA,
        "identity": identity,
        "release": release_info,
        "release_candidate_namespace": manifest["candidate_namespace"],
        "release_contract": release_contract,
        "profile_digest": _canonical_digest(profile),
        "readiness_profile_digest": _canonical_digest(readiness),
        "effective_config": config,
        "source_facts": source_facts,
        "workflow": {
            "capability_id": capability,
            "local_path": str(workflow_local),
            "remote_path": remote_workflow,
            "inputs": dict(workflow_inputs),
        },
        "workflow_bundle": bundle_plan,
        "readiness_profile": readiness,
        "tree_excludes": sorted(_TREE_EXCLUDES),
    }
    plan["plan_digest"] = _canonical_digest(plan)
    return plan


def _journal_path(journal_dir: Path, operation_id: str) -> Path:
    key = hashlib.sha256(operation_id.encode()).hexdigest()
    return journal_dir.expanduser() / f"{key}.json"


def _tree_probe(value: Mapping[str, Any], expected: Mapping[str, Any], *, name: str) -> bool:
    if value.get("exists") is False:
        return False
    digest = value.get("tree_digest")
    if not isinstance(digest, str) or type(value.get("file_count")) is not int:
        raise WorkerPreparationError(f"remote {name} tree observation was empty or malformed")
    if digest != expected["tree_digest"] or value["file_count"] != expected["file_count"]:
        return False
    return True


async def _stage_step(
    *,
    name: str,
    expected: Mapping[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    transport: WorkerPreparationTransport,
    refresh: bool = False,
) -> None:
    steps = journal.setdefault("steps", {})
    record = steps.get(name)
    if record is not None and record.get("status") == "verified":
        observed = await transport.probe_tree(str(expected["remote_path"]))
        if not _tree_probe(observed, expected, name=name) and not refresh:
            raise StagingEvidenceStaleError(
                f"previously verified remote {name} bytes changed; refusing stale evidence"
            )
        if _tree_probe(observed, expected, name=name):
            return
    # A crash may happen after upload but before the completion write. Reuse
    # only if actual bytes already match the selected source inventory.
    observed = await transport.probe_tree(str(expected["remote_path"]))
    if not _tree_probe(observed, expected, name=name):
        remote_path = _remote_absolute(str(expected["remote_path"]), label="owned stage path")
        if not remote_path.startswith(str(PurePosixPath(journal["owned_root"])) + "/"):
            raise WorkerPreparationError("refusing to clear a path outside this operation's owned stage root")
        if observed.get("exists") is not False:
            await transport.remove_owned_tree(remote_path, str(journal["owned_root"]))
        journal["steps"][name] = {
            "status": "intent",
            "tree_digest": expected["tree_digest"],
            "remote_path": remote_path,
        }
        _atomic_private_write(journal_path, journal)
        try:
            await transport.upload_path(Path(str(expected["local_path"])), remote_path)
        except WorkerPreparationError:
            raise
        except Exception as exc:
            raise WorkerPreparationError(f"upload of selected {name} source failed: {exc}") from exc
        observed = await transport.probe_tree(remote_path)
        if not _tree_probe(observed, expected, name=name):
            raise WorkerPreparationError(f"uploaded {name} did not match its selected content inventory")
    journal["steps"][name] = {
        "status": "verified",
        "tree_digest": expected["tree_digest"],
        "file_count": expected["file_count"],
        "remote_path": expected["remote_path"],
    }
    _atomic_private_write(journal_path, journal)


def _child_request(plan: Mapping[str, Any]) -> dict[str, Any]:
    readiness_profile = plan["readiness_profile"]
    expected_tree_digests = {
        "astrid_root": plan["source_facts"]["astrid"]["tree_digest"],
        "vibecomfy_root": plan["source_facts"]["vibecomfy"]["tree_digest"],
    }
    return {
        "effective_config": dict(plan["effective_config"]),
        "readiness_profile": readiness_profile,
        "workflow_path": plan["workflow"]["remote_path"],
        "workflow_inputs": dict(plan["workflow"]["inputs"]),
        "capability_id": plan["workflow"]["capability_id"],
        "workflow_bundle": (
            None
            if plan.get("workflow_bundle") is None
            else {
                "root": plan["workflow_bundle"]["remote_root"],
                "members": dict(plan["workflow_bundle"]["members"]),
            }
        ),
        "expected_tree_digests": expected_tree_digests,
        "tree_excludes": list(plan["tree_excludes"]),
        "release_contract": dict(plan["release_contract"]),
    }


async def _verify_remote_cpu_evidence(
    *, plan: Mapping[str, Any], transport: WorkerPreparationTransport
) -> dict[str, Any]:
    request = _child_request(plan)
    try:
        observed = await transport.child_check(request)
    except WorkerPreparationError:
        raise
    except Exception as exc:
        raise WorkerPreparationError(f"child CPU readiness observation failed: {exc}") from exc
    if not isinstance(observed, Mapping) or observed.get("status") != "cpu_ready":
        raise WorkerPreparationError("child CPU readiness observation was empty or not ready")
    if observed.get("gpu_qualified") is not False:
        raise WorkerPreparationError("CPU preparation must never assert GPU qualification")
    if observed.get("capability_id") != plan["workflow"]["capability_id"]:
        raise WorkerPreparationError("child readiness was produced for another capability")
    expected_paths = {
        key: str(Path(value))
        for key, value in plan["effective_config"].items()
        if key in {"astrid_root", "vibecomfy_root", "comfy_root", "model_root", "output_root", "candidate_namespace", "launcher_path"}
    }
    if observed.get("effective_paths") != expected_paths:
        raise WorkerPreparationError("child effective paths differ from the selected configuration")
    if not isinstance(observed.get("model_root"), Mapping) or not isinstance(
        observed.get("workflow_preflight"), Mapping
    ):
        raise WorkerPreparationError("child model or canonical workflow evidence is missing")
    return dict(observed)


async def prepare_worker(
    *,
    claim_handle_path: Path,
    journal_dir: Path,
    provider_account_ref: str,
    release_manifest_path: Path,
    release_manifest_sha256: str,
    release_id: str,
    resolved_compute_profile: Mapping[str, Any],
    readiness_profile: Mapping[str, Any],
    astrid_source: Path,
    vibecomfy_source: Path,
    comfy_root: str,
    custom_nodes_lock_path: str,
    custom_node_roots: Mapping[str, str],
    python_executable: str,
    engine_mode: str,
    workflow_path: Path,
    workflow_bundle_path: Path | None = None,
    workflow_inputs: Mapping[str, Any],
    capability_id: str,
    transport: WorkerPreparationTransport,
    refresh: bool = False,
) -> dict[str, Any]:
    """Stage selected sources and verify the mounted Comfy release and CPU facts.

    The operation and provider/volume custody come from the exact P1 handle.
    The caller must pass an explicitly selected manifest and its verified byte
    digest. No activation, claim, inference, or GPU qualification occurs.
    """
    handle_identity = _claim_identity(claim_handle_path, provider_account_ref)
    selected_readiness = json.loads(json.dumps(readiness_profile, sort_keys=True, ensure_ascii=False))
    plan = _build_plan(
        claim_handle_path=claim_handle_path,
        provider_account_ref=provider_account_ref,
        release_manifest_path=release_manifest_path,
        release_manifest_sha256=release_manifest_sha256,
        release_id=release_id,
        resolved_compute_profile=resolved_compute_profile,
        readiness_profile=selected_readiness,
        astrid_source=astrid_source,
        vibecomfy_source=vibecomfy_source,
        comfy_root=comfy_root,
        custom_nodes_lock_path=custom_nodes_lock_path,
        custom_node_roots=custom_node_roots,
        python_executable=python_executable,
        engine_mode=engine_mode,
        workflow_path=workflow_path,
        workflow_bundle_path=workflow_bundle_path,
        workflow_inputs=workflow_inputs,
        capability_id=capability_id,
    )
    plan["readiness_profile"] = selected_readiness
    journal_path = _journal_path(journal_dir, str(handle_identity["operation_id"]))
    with _locked_journal(journal_path):
        journal = _load_journal(journal_path)
        if journal is not None:
            if journal.get("identity") != handle_identity:
                raise PreparationJournalError("preparation journal belongs to different P1 custody")
            if journal.get("plan_digest") != plan["plan_digest"]:
                raise PreparationJournalError("selected release/profile/config changed; refusing journal reuse")
            if refresh and journal.get("status") == "cpu_ready":
                journal.pop("cpu_readiness", None)
                journal["status"] = "preparing"
                journal["refresh_started_at"] = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
                _atomic_private_write(journal_path, journal)
        else:
            journal = {
                "schema": _SCHEMA,
                "identity": handle_identity,
                "plan_digest": plan["plan_digest"],
                "release": plan["release"],
                "release_contract": plan["release_contract"],
                "profile_digest": plan["profile_digest"],
                "readiness_profile_digest": plan["readiness_profile_digest"],
                "owned_root": _remote_owned_root(handle_identity),
                "status": "preparing",
                "steps": {},
            }
            _atomic_private_write(journal_path, journal)
        # The complete profile is sent only across the command boundary and is
        # never copied into the durable journal.
        for name in ("astrid", "vibecomfy"):
            await _stage_step(
                name=name,
                expected=plan["source_facts"][name],
                journal=journal,
                journal_path=journal_path,
                transport=transport,
                refresh=refresh,
            )
        bundle = plan.get("workflow_bundle")
        if isinstance(bundle, Mapping):
            await _stage_step(
                name="workflow_bundle",
                expected={
                    "local_path": bundle["local_path"],
                    "remote_path": bundle["remote_root"],
                    "tree_digest": bundle["tree_digest"],
                    "file_count": bundle["file_count"],
                },
                journal=journal,
                journal_path=journal_path,
                transport=transport,
                refresh=refresh,
            )
        cpu_facts = await _verify_remote_cpu_evidence(plan=plan, transport=transport)
        previous_facts = journal.get("cpu_readiness")
        if previous_facts is not None and not refresh and cpu_facts != previous_facts:
            raise StagingEvidenceStaleError("child CPU facts drifted from the recorded preparation evidence")
        journal["cpu_readiness"] = cpu_facts
        journal["status"] = "cpu_ready"
        from datetime import datetime, timezone

        journal["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_private_write(journal_path, journal)
        return _public_result(journal, journal_path)


def _public_result(journal: Mapping[str, Any], journal_path: Path) -> dict[str, Any]:
    facts = journal.get("cpu_readiness")
    if not isinstance(facts, Mapping):
        raise PreparationJournalError("prepared journal has no CPU readiness facts")
    return {
        "schema": _SCHEMA,
        "status": "cpu_ready",
        "operation_id": journal["identity"]["operation_id"],
        "pod_id": journal["identity"]["pod_id"],
        "network_volume_id": journal["identity"]["volume_id"],
        "release_id": journal["release"]["release_id"],
        "release_state": facts.get("release_facts", {}).get("manifest_state"),
        "plan_digest": journal["plan_digest"],
        "journal_path": str(journal_path),
        "cpu_readiness": dict(facts),
        "gpu_qualified": False,
        "worker_qualified": False,
        "qualification_scope": "cpu_only_ready_for_gpu_qualification",
    }


__all__ = [
    "PreparationJournalError",
    "RunPodPreparationTransport",
    "StagingEvidenceStaleError",
    "WorkerPreparationError",
    "WorkerPreparationTransport",
    "prepare_worker",
]
