"""Host-local request estimation; transport/catalog schemas remain Runtime-owned.

Preparation is a prediction, never an authorization token. Only recomputation
from an admitted definition/source may authorize the pair for producer execution.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import keyword
import os
import re
import selectors
import shutil
import subprocess
import sys
import sysconfig
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.core.foundation.hash import canonical_json_digest

SCHEMA_VERSION = 1
# Runtime's signed 64-bit envelope is further restricted to interoperable JSON
# integers. Check the sum too, rather than accepting two individually safe terms.
MAX_STORAGE_BYTES = 9007199254740991


class StorageEstimatorError(ValueError):
    pass


@dataclass(frozen=True)
class StorageEstimateRequest:
    capability_id: str
    capability_digest: str
    schema_version: int
    spec: Mapping[str, Any]
    input_facts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class StoragePreparation:
    request: StorageEstimateRequest
    spec_digest: str
    source_digest: str
    estimator: Mapping[str, Any]
    storage_estimate: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Semantic preparation payload for A's existing transport adapter.

        This does not register a Runtime schema, endpoint or authority token.
        """
        payload = copy.deepcopy({
            "capability_id": self.request.capability_id,
            "capability_digest": self.request.capability_digest,
            "schema_version": self.request.schema_version,
            "spec": self.request.spec, "input_facts": list(self.request.input_facts),
            "spec_digest": self.spec_digest, "source_digest": self.source_digest,
            "estimator": self.estimator, "storage_estimate": self.storage_estimate,
        })
        payload["preparation_digest"] = preparation_digest(self)
        return payload


def strict_json(value: Any) -> None:
    """Reject coercions before using the existing canonical task encoding."""
    def check(item):
        if type(item) is dict:
            if any(type(k) is not str for k in item):
                raise StorageEstimatorError("non-string JSON key")
            for nested in item.values():
                check(nested)
        elif type(item) is list:
            for nested in item:
                check(nested)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise StorageEstimatorError("non-JSON estimation request")
    try:
        check(value)
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise StorageEstimatorError("estimation requires finite strict JSON") from exc


def validate_pair(value: Any) -> dict[str, int]:
    if (type(value) is not dict or set(value) != {"scratch_bytes", "output_bytes"}
            or any(type(n) is not int or not 0 <= n <= MAX_STORAGE_BYTES for n in value.values())
            or sum(value.values()) > MAX_STORAGE_BYTES):
        raise StorageEstimatorError("invalid or overflowing whole-task storage estimate")
    return dict(value)


def _declared_estimator_descriptor(record, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the manifest-owned J041 descriptor without importing its code."""
    if (set(raw) != {"mode", "estimator", "implementation"}
            or raw.get("mode") != "request_estimated"):
        raise StorageEstimatorError("missing or unsupported request storage estimator mode")
    identity = raw.get("estimator")
    implementation = raw.get("implementation")
    if (not isinstance(identity, Mapping)
            or set(identity) != {"id", "version", "implementation_digest"}
            or type(identity.get("id")) is not str
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identity["id"])
            or type(identity.get("version")) is not int
            or not 0 < identity["version"] <= MAX_STORAGE_BYTES
            or type(identity.get("implementation_digest")) is not str
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["implementation_digest"])):
        raise StorageEstimatorError("missing or malformed request storage estimator identity")
    if (not isinstance(implementation, Mapping)
            or set(implementation) != {"module", "function"}
            or type(implementation.get("module")) is not str
            or type(implementation.get("function")) is not str
            or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                implementation["module"],
            )
            or not implementation["function"].isidentifier()
            or keyword.iskeyword(implementation["function"])):
        raise StorageEstimatorError("missing or malformed request storage estimator implementation")

    declaration = record.definition.metadata.get("action_declaration", {})
    if (not isinstance(declaration, Mapping)
            or not isinstance(declaration.get("metadata"), Mapping)
            or declaration["metadata"].get("storage_estimator") != raw):
        raise StorageEstimatorError("estimator must belong to the admitted action declaration")
    invocation = record.definition.metadata.get("action_invocation")
    if (not isinstance(invocation, Mapping) or invocation.get("kind") not in {"python", "command"}
            or declaration.get("invocation") != invocation):
        raise StorageEstimatorError("estimator requires the same admitted action implementation")

    pack_id = record.definition.metadata.get("source_pack")
    root = record.source_root.resolve()
    if (type(pack_id) is not str or not pack_id
            or Path(record.definition.metadata.get("pack_root", "")).resolve() != root):
        raise StorageEstimatorError("estimator action source root differs")
    package_prefix = f"astrid.packs.{pack_id}."
    module = implementation["module"]
    if not module.startswith(package_prefix):
        raise StorageEstimatorError("estimator module is outside the admitted action pack")
    module_parts = module[len(package_prefix):].split(".")
    if len(module_parts) < 2 or module_parts[0] != "actions":
        raise StorageEstimatorError("estimator module is outside the admitted action source")
    path = Path(*module_parts[:-1], module_parts[-1] + ".py")
    source = (root / path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise StorageEstimatorError("estimator source missing or escapes action source fence")

    try:
        text = source.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise StorageEstimatorError("estimator source cannot be read as Python") from exc
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name == implementation["function"]]
    if len(functions) != 1 or functions[0].end_lineno is None:
        raise StorageEstimatorError("declared estimator function is not a unique module function")
    function = functions[0]
    first_line = min((decorator.lineno for decorator in function.decorator_list),
                     default=function.lineno)
    function_source = "".join(text.splitlines(keepends=True)[first_line - 1:function.end_lineno])
    actual_digest = "sha256:" + hashlib.sha256(function_source.encode("utf-8")).hexdigest()
    if actual_digest != identity["implementation_digest"]:
        raise StorageEstimatorError("declared estimator implementation digest does not match source")

    # This declaration identifies the producer helper. Host invocation is
    # wired separately; only the public identity is projected here.
    return {key: identity[key] for key in ("id", "version", "implementation_digest")}


def estimator_descriptor(record) -> dict[str, Any]:
    """Read declarations only; never import a producer during discovery."""
    raw = record.definition.metadata.get("storage_estimator")
    if isinstance(raw, Mapping) and ({"mode", "estimator", "implementation"} & set(raw)):
        return _declared_estimator_descriptor(record, raw)
    if (not isinstance(raw, dict) or set(raw) != {
            "schema_version", "id", "version", "policy_version", "path", "function"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION
            or type(raw["version"]) is not int or not 0 < raw["version"] <= MAX_STORAGE_BYTES
            or any(type(raw[k]) is not str or not raw[k] for k in
                   ("id", "policy_version", "path", "function"))):
        raise StorageEstimatorError("missing or unsupported request storage estimator")
    declaration = record.definition.metadata.get("action_declaration", {})
    if (not isinstance(declaration, Mapping) or not isinstance(declaration.get("metadata"), Mapping)
            or declaration["metadata"].get("storage_estimator") != raw):
        raise StorageEstimatorError("estimator must belong to the admitted action declaration")
    invocation = record.definition.metadata.get("action_invocation")
    if (not isinstance(invocation, Mapping) or invocation.get("kind") not in {"python", "command"}
            or declaration.get("invocation") != invocation):
        raise StorageEstimatorError("estimator requires the same admitted action implementation")
    path = Path(raw["path"])
    if (path.is_absolute() or "\\" in raw["path"] or any(ord(c) < 32 for c in raw["path"])
            or any(part in ("", ".", "..") for part in raw["path"].split("/"))
            or not path.parts or path.parts[0] != "actions" or path.suffix != ".py"
            or not raw["function"].isidentifier() or keyword.iskeyword(raw["function"])):
        raise StorageEstimatorError("invalid estimator path/function")
    root = record.source_root.resolve()
    if Path(record.definition.metadata.get("pack_root", "")).resolve() != root:
        raise StorageEstimatorError("estimator action source root differs")
    source = (root / path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise StorageEstimatorError("estimator source missing or escapes action source fence")
    return {**raw, "implementation_digest": "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()}


def preparation_digest(prepared: StoragePreparation) -> str:
    if type(prepared) is not StoragePreparation or type(prepared.request) is not StorageEstimateRequest:
        raise StorageEstimatorError("invalid storage preparation")
    validate_pair(prepared.storage_estimate)
    payload = {"capability_id": prepared.request.capability_id,
               "capability_digest": prepared.request.capability_digest,
               "schema_version": prepared.request.schema_version,
               "spec": prepared.request.spec, "input_facts": list(prepared.request.input_facts),
               "spec_digest": prepared.spec_digest, "source_digest": prepared.source_digest,
               "estimator": prepared.estimator, "storage_estimate": prepared.storage_estimate}
    strict_json(payload)
    return "sha256:" + canonical_json_digest(payload)


def validate_request(record, request: StorageEstimateRequest) -> None:
    if (type(request.schema_version) is not int or request.schema_version != SCHEMA_VERSION
            or request.capability_id != record.id or request.capability_digest != record.capability_digest):
        raise StorageEstimatorError("estimator capability digest/id or schema version mismatch")
    strict_json(request.spec)
    if type(request.spec) is not dict:
        raise StorageEstimatorError("estimator requires a canonical spec object")
    if request.spec.get("capability_id", record.id) != record.id:
        raise StorageEstimatorError("canonical spec capability mismatch")
    params = request.spec.get("params")
    if not isinstance(params, dict) or not isinstance(request.spec.get("output_policy"), dict):
        raise StorageEstimatorError("estimator requires canonical params and output_policy")
    # Defaults are bound once by the caller's canonical task builder. Never
    # inject/drop values here, which would estimate a different execution spec.
    for port in record.definition.inputs:
        if (port.required or port.default is not None) and port.name not in params:
            raise StorageEstimatorError(f"canonical params missing required/defaulted {port.name}")
    from astrid.sdk.actions import validate_action_inputs_definition
    validate_action_inputs_definition(record.definition, params)
    if type(request.input_facts) is not tuple:
        raise StorageEstimatorError("immutable input facts must be a tuple")
    seen = set()
    for fact in request.input_facts:
        strict_json(fact)
        if (type(fact) is not dict or set(fact) != {"object_id", "size_bytes"}
                or type(fact["object_id"]) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", fact["object_id"])
                or fact["object_id"] in seen or type(fact["size_bytes"]) is not int
                or not 0 <= fact["size_bytes"] <= MAX_STORAGE_BYTES):
            raise StorageEstimatorError("invalid immutable input identity/size facts")
        seen.add(fact["object_id"])


def _invoke(root: Path, descriptor: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, int]:
    """Executed only in the read-only OS-confined helper, over private pipes."""
    from astrid.core.execution.executor.runner import _PackSourceFinder, _PackSourceLoader

    class PinnedLoader(_PackSourceLoader):
        def __init__(self, source, directory, expected):
            super().__init__(source, directory)
            self.expected = expected

        def exec_module(self, module):
            if self.source is not None:
                content = self.source.read_bytes()
                if hashlib.sha256(content).hexdigest() != self.expected:
                    raise StorageEstimatorError("estimator imported source bytes changed")
                module.__file__ = str(self.source)
                exec(compile(content, str(self.source), "exec"), module.__dict__)

    class PinnedFinder(_PackSourceFinder):
        def find_spec(self, fullname, path=None, target=None):
            spec = super().find_spec(fullname, path, target)
            if spec is not None:
                # Bind the requested logical import path as well as its bytes.
                # Resolving an internal symlink must not select another admitted
                # module after inventory admission.
                parts = fullname.split(".")[1:]
                base = root.joinpath(*(self.path_names.get(part, part) for part in parts))
                logical = base / "__init__.py" if spec.submodule_search_locations is not None else Path(str(base) + ".py")
                expected = payload["source_files"].get(str(logical.relative_to(root)))
                spec.loader = PinnedLoader(spec.loader.source, spec.loader.directory, expected)
            return spec

    prefix = "_astrid_estimator_" + uuid.uuid4().hex
    path = Path(descriptor["path"])
    names, parts = {}, []
    for part in (*path.parts[:-1], path.stem):
        name = part if part.isidentifier() and not keyword.iskeyword(part) else "_path_" + uuid.uuid4().hex
        if name != part:
            names[name] = part
        parts.append(name)
    finder = PinnedFinder(prefix, root, names)
    sys.meta_path.insert(0, finder)
    try:
        function = getattr(importlib.import_module(prefix + "." + ".".join(parts)), descriptor["function"], None)
        if not callable(function):
            raise StorageEstimatorError("declared estimator function is unavailable")
        return validate_pair(function(spec=payload["spec"], input_facts=payload["input_facts"]))
    finally:
        sys.meta_path.remove(finder)
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]


def run_source_estimator(record, descriptor: Mapping[str, Any], request: StorageEstimateRequest,
                         *, timeout: float, output_limit: int) -> dict[str, int]:
    """No workspace, credentials, file writes, network or descendant processes.

    Reuse the host's source importer and OS backend; unavailable confinement
    refuses rather than executing producer code inside the credentialed host.
    """
    from .generic_host import (
        _filesystem_sandbox_profile,
        _FilesystemSandboxPolicy,
        _python_runtime_library,
    )
    if sys.platform != "darwin" or not shutil.which("/usr/bin/sandbox-exec"):
        raise StorageEstimatorError("request estimation requires the read-only macOS sandbox backend")
    root = record.source_root.resolve()
    entries = [(str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
               for path in sorted(root.rglob("*"))
               if path.is_file() and "__pycache__" not in path.relative_to(root).parts
               and path.suffix not in {".pyc", ".pyo"}]
    captured = canonical_json_digest([{"root": str(root), "digest": canonical_json_digest(entries)}])
    if captured != record.source_digest:
        raise StorageEstimatorError("admitted estimator source inventory changed")
    source_files = dict(entries)
    if "sha256:" + source_files.get(descriptor["path"], "") != descriptor["implementation_digest"]:
        raise StorageEstimatorError("admitted estimator implementation digest changed")
    host_root = Path(__file__).resolve().parents[3]
    roots = tuple(dict.fromkeys(p.resolve() for p in (
        host_root, Path(sys.base_prefix), Path(sys.prefix), Path('/usr/lib'), Path('/System/Library'),
        Path(sysconfig.get_path('stdlib')), Path(sysconfig.get_path('purelib')),
    ) if p.is_dir()))
    executable = Path(sys.executable)
    # Reuse the Host's exact interpreter bootstrap files. The selected pyenv
    # Python depends on this read-only dyld artifact outside sys.base_prefix;
    # no dependency directory, project write or ambient Homebrew root is granted.
    python_library = _python_runtime_library(Path(sys.base_prefix).resolve(strict=True))
    gettext = Path("/opt/homebrew/opt/gettext/lib/libintl.8.dylib")
    bootstrap = (executable, *((python_library,) if python_library is not None else ()),
                 *((gettext,) if gettext.is_file() else ()))
    profile = _filesystem_sandbox_profile(_FilesystemSandboxPolicy(
        read_roots=(root,), bootstrap_files=bootstrap, bootstrap_roots=roots,
        private_write_root=root), root)
    # The shared builder requires an existing private root; preparation grants
    # zero writes, even there. All IPC is stdin/stdout/stderr, with no files.
    profile = '\n'.join(line for line in profile.splitlines() if not line.startswith('(allow file-write'))
    profile = profile.replace('(allow process*)', '(allow process-exec)\n(deny process-fork)')
    code = (
        'import sys,json;sys.path.insert(0,sys.argv[1]);sys.path.extend(json.loads(sys.argv[2]));'
        'from astrid.core.execution.storage_estimator import _invoke;'
        'from pathlib import Path;'
        'from astrid.core.execution.generic_host import _verify_admitted_source;'
        'p=json.load(sys.stdin);_verify_admitted_source(p["admission"]);'
        'r=_invoke(Path(p["root"]),p["descriptor"],p);'
        'sys.stdout.write(json.dumps(r,allow_nan=False))'
    )
    payload = {"root": str(root), "descriptor": dict(descriptor), "spec": request.spec,
               "input_facts": list(request.input_facts), "source_files": source_files,
               "admission": {"source_digest": record.source_digest, "source_roots": [str(root)]}}
    argv = ['/usr/bin/sandbox-exec', '-p', profile, str(executable),
            '-I', '-S', '-B', '-c', code, str(host_root),
            json.dumps(list(dict.fromkeys([sysconfig.get_path('purelib'), sysconfig.get_path('platlib')])))]
    # stdout/stderr are producer-controlled. Enforce the existing evidence cap
    # while draining, rather than buffering unbounded communicate() output.
    data = json.dumps(payload, allow_nan=False, ensure_ascii=False).encode('utf-8')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=root,
                                   env={"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                                        "PYTHONDONTWRITEBYTECODE": "1"})
    except OSError as exc:
        raise StorageEstimatorError("admitted request estimator could not start") from exc
    selector = selectors.DefaultSelector()
    sent, total = 0, 0
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        for pipe in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_WRITE if pipe is process.stdin else selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StorageEstimatorError("admitted request estimator exceeded execution deadline")
            for key, _ in selector.select(min(remaining, 0.1)):
                pipe = key.fileobj
                if pipe is process.stdin:
                    sent += os.write(pipe.fileno(), data[sent:sent + 65536])
                    if sent == len(data):
                        selector.unregister(pipe)
                        pipe.close()
                else:
                    chunk = os.read(pipe.fileno(), 65536)
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    total += len(chunk)
                    if total > output_limit:
                        raise StorageEstimatorError("admitted request estimator exceeded evidence cap")
                    if pipe is process.stdout:
                        output.extend(chunk)
        if process.wait(timeout=max(0.001, deadline - time.monotonic())):
            raise StorageEstimatorError("admitted request estimator refused or failed")
        return validate_pair(json.loads(output))
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        if isinstance(exc, StorageEstimatorError):
            raise
        raise StorageEstimatorError("admitted request estimator failed") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


def prepare(record, request: StorageEstimateRequest, *, timeout: float, output_limit: int) -> StoragePreparation:
    from .generic_host import _admitted_source_roots, _capability_digest, _verify_admitted_source
    # Detach before validation so a caller cannot mutate between validation
    # and computation. No normalization, filtering or default injection.
    frozen = copy.deepcopy(request)
    descriptor = estimator_descriptor(record)
    if not {"path", "function"}.issubset(descriptor):
        raise StorageEstimatorError("declared producer estimator is not wired to host invocation")
    validate_request(record, frozen)
    admission = {"source_digest": record.source_digest,
                 "source_roots": [str(p) for p in _admitted_source_roots(record.source_root, record.definition)]}
    _verify_admitted_source(admission)
    if _capability_digest(record.definition.to_dict()) != record.capability_digest:
        raise StorageEstimatorError("admitted estimator definition digest changed")
    pair = run_source_estimator(record, descriptor, frozen, timeout=timeout, output_limit=output_limit)
    _verify_admitted_source(admission)
    identity = {key: descriptor[key] for key in ("id", "version", "implementation_digest")}
    return StoragePreparation(frozen, "sha256:" + canonical_json_digest(frozen.spec),
                              "sha256:" + record.source_digest, identity, pair)
