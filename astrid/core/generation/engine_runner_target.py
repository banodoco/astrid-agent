"""Installed finite target helpers for the canonical runner; no transport/provider.

This module is invoked ON THE TARGET by ExecutionContext.run. Its filesystem and
subprocess operations must never be called by a controller for a remote root.
Service hosting is deliberately A2's engine_execution responsibility. This module
contains neither a model downloader nor a second environment installer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from . import engine_recipe as recipes
from .engine_contracts import EngineError, IntegrityError, RecipeValidationError, UnsafePathError
from .engine_runner import _HELPER_SCHEMA, _json, _root, _sha, helper_manifest

_OPERATIONS = {"identity", "empty-source", "node-lock", "lock-inputs", "commit-lock",
               "install-locks", "environment-spec", "models-config", "models-config-check"}
_ENV_OPERATIONS = {"node-lock", "lock-inputs", "commit-lock", "install-locks", "environment-spec"}


def _safe(root, *relative):
    path = root.joinpath(*relative)
    # Check EVERY existing component, including the root. Files/dirs generated
    # here are never permitted to traverse a target symlink.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise UnsafePathError("target helper path traverses a symlink")
    if not path.is_relative_to(root):
        raise UnsafePathError("target helper path escapes installation")
    return path


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    if temporary.is_symlink():
        raise UnsafePathError("unsafe target helper temporary file")
    with temporary.open("wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _health_facts(health, object_info, required_nodes):
    """Pure evidence projection for A2 AFTER owned same-origin HTTP probes.

    A2 supplies parsed JSON only after successful bounded, redirect-free HTTP
    responses from the exact owned process. The full object_info body stays on
    the target; the command protocol carries only this bounded projection.
    """
    digest = None
    if isinstance(object_info, dict):
        try:
            digest = _sha(object_info)
        except (ValueError, TypeError, UnicodeError):
            pass
    return {"healthy": isinstance(health, dict) and digest is not None,
            "object_info_sha256": digest,
            "required_nodes_present": digest is not None and all(n in object_info for n in required_nodes)}


def _entries(text):
    """Read uv pip-compile hash-bearing logical requirements, fail closed."""
    result, pending = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending += " " + stripped.removesuffix("\\").strip()
        if stripped.endswith("\\"):
            continue
        value, pending = pending.strip(), ""
        if value.startswith("--"):
            raise IntegrityError("requirements lock contains an unsupported directive")
        hashes = re.findall(r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)", value)
        requirement = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)", "", value).strip()
        if (not hashes or "--hash" in requirement or "\\" in requirement
                or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?\s*(?:==|@)", requirement)):
            raise IntegrityError("requirements lock lacks exact hash-bearing requirements")
        result.append((requirement, hashes, value))
    if pending or not result:
        raise IntegrityError("requirements lock is empty or incomplete")
    return result


def receipt_name(wheel_name):
    return f"install-receipt-{wheel_name}.json"


def _paths(root, environment):
    directory = _safe(root, ".astrid-engine", "environments", environment.name)
    return directory, _safe(root, ".astrid-engine", "environments", environment.name, "requirements.lock")


def _lock_identity(recipe, helper_digest, environment, lock, *, digest=None):
    return {"schema": "astrid.requirements-lock.v1", "environment": environment.name,
            "recipe_sha256": recipes.recipe_digest(recipe), "helper_sha256": helper_digest,
            "requirements_lock_sha256": _hash(lock) if digest is None else digest}


def _validate_lock(recipe, helper_digest, environment, root):
    directory, lock = _paths(root, environment)
    _entries(lock.read_text(encoding="utf-8"))
    meta = _safe(root, ".astrid-engine", "environments", environment.name, "lock.json")
    if not meta.is_file() or json.loads(meta.read_text()) != _lock_identity(recipe, helper_digest, environment, lock):
        raise IntegrityError("persisted requirements lock has no matching input identity")
    if environment.requirements.mode == "packaged" and _hash(lock) != environment.requirements.packaged_lock.sha256:
        raise IntegrityError("target requirements lock differs from packaged lock")
    return directory, lock


# Shared static snippet: sha256 of an installed distribution's own RECORD file.
_RECORD_SHA = r'''
import hashlib
def record_sha256(distribution):
    records = [f for f in (distribution.files or ()) if f.name == 'RECORD' and len(f.parts) == 2]
    assert len(records) == 1
    return hashlib.sha256(distribution.locate_file(records[0]).read_bytes()).hexdigest()
'''

# Static code run in the selected environment right after a hash-pinned install.
# It only reports each installed wheel's RECORD digest; the runner persists the
# install receipt. Never populated from recipe code.
_RECEIPT_SCRIPT = _RECORD_SHA + r'''
import importlib.metadata as metadata, json, sys
expected=json.loads(sys.argv[1])
digests={}
for name, wheel in expected['wheels'].items():
    distribution=metadata.distribution(name)
    assert distribution.version == wheel['version'], name
    digests[name]=record_sha256(distribution)
print(json.dumps({'record_sha256': digests}))
'''

# Static product verification code, executed in the selected environment. This
# is never populated from recipe code and does not install or repair anything.
_ENV_PROBE = _RECORD_SHA + r'''
import importlib, importlib.metadata as metadata, json, sys
from urllib.parse import unquote, urlsplit
from packaging.requirements import Requirement
def receipt_matches(name, wheel, distribution):
    # uv records no hash in direct_url.json; the hash-pinned install verified the
    # bytes, so accept the runner's install receipt if it still describes this dist.
    try:
        with open(wheel['receipt'], 'rb') as stream:
            receipt=json.loads(stream.read())
        return (isinstance(receipt, dict)
                and receipt.get('name') == name
                and receipt.get('version') == wheel['version']
                and unquote(str(receipt.get('url',''))) == unquote(wheel['url'])
                and receipt.get('sha256') == wheel['sha256']
                and receipt.get('record_sha256') == record_sha256(distribution))
    except Exception:
        return False
expected=json.loads(sys.argv[1])
assert sys.version.split()[0] == expected['python']
versions={}
for text in expected['requirements']:
    req=Requirement(text)
    if req.marker and not req.marker.evaluate():
        continue
    if req.name.lower().replace('_','-') in expected['wheels']:
        continue
    installed=metadata.version(req.name)
    assert req.url is not None or installed in req.specifier, req.name
    versions[req.name]=installed
for name, wheel in expected['wheels'].items():
    distribution=metadata.distribution(name)
    assert distribution.version == wheel['version'], name
    direct=json.loads(distribution.read_text('direct_url.json') or '{}')
    assert unquote(direct.get('url','')) == unquote(wheel['url']), name
    archive=direct.get('archive_info',{})
    assert (archive.get('hashes',{}).get('sha256') == wheel['sha256'] or archive.get('hash') == 'sha256='+wheel['sha256']
            or receipt_matches(name, wheel, distribution)), name
    module=importlib.import_module(name)
    assert module.__version__ == wheel['version'], name
for constraint in expected['constraints']:
    req=Requirement(constraint)
    assert metadata.version(req.name) in req.specifier, req.name
for source in expected['sources']:
    for name in source['modules']:
        importlib.import_module(name)
    found=False
    for distribution in metadata.distributions():
        raw=distribution.read_text('direct_url.json')
        if raw and unquote(urlsplit(json.loads(raw).get('url','')).path).rstrip('/') == source['path']:
            found=True
    assert found, source['path']
if 'torch' in expected['wheels']:
    import torch, cv2
    assert torch.version.cuda == expected['cuda']
    assert not torch.cuda.is_initialized()
print(json.dumps({'valid': True}))
'''


def _environment_spec(recipe, environment, root, helper_digest):
    _, lock = _validate_lock(recipe, helper_digest, environment, root)
    directory, _ = _paths(root, environment)
    wheels = {w.name: {"version": w.version, "url": w.url, "sha256": w.sha256,
                       "receipt": str(directory / receipt_name(w.name))}
              for w in environment.final_wheels}
    expected = {"python": environment.python_version, "wheels": wheels,
                "constraints": list(environment.requirements.constraints),
                "requirements": [entry[0] for entry in _entries(lock.read_text())],
                "sources": [{"path": str(_safe(root, next(s.relative_path for s in recipe.sources
                                                         if s.name == install.source))),
                             "modules": list(install.check_modules)}
                            for install in environment.source_installs],
                "cuda": {"cu124": "12.4", "cu128": "12.8", "cu130": "13.0"}[recipe.platform.rsplit("-", 1)[1]]}
    # The runner executes the static probe through context.run, so EVERY child
    # process has the same context deadline, output bound and custody boundary.
    return {"expected": expected, "requirements_lock_sha256": _hash(lock)}


def dispatch(operation, payload):
    if operation not in _OPERATIONS:
        raise RecipeValidationError("unknown engine helper operation")
    keys = {"schema", "engine", "platform", "root", "recipe_sha256", "helper_sha256"}
    if operation in _ENV_OPERATIONS:
        keys.add("environment")
    if operation == "empty-source":
        keys.add("source")
    if not isinstance(payload, dict) or set(payload) != keys or payload["schema"] != _HELPER_SCHEMA:
        raise RecipeValidationError("invalid engine helper request")
    recipe = recipes.load_recipe(payload["engine"], payload["platform"])
    if not recipe.platform.startswith("linux-"):
        raise RecipeValidationError("Linux helper requires a Linux recipe")
    digest = _sha(helper_manifest(recipe))
    if payload["recipe_sha256"] != recipes.recipe_digest(recipe) or payload["helper_sha256"] != digest:
        raise IntegrityError("engine helper code/data identity differs")
    root = Path(_root(payload["root"], recipe.platform))
    _safe(root)
    environment = None
    if operation in _ENV_OPERATIONS:
        environment = next((e for e in recipe.environments if e.name == payload["environment"]), None)
        if environment is None:
            raise RecipeValidationError("unknown named environment")
    if operation == "identity":
        facts = {"helper_sha256": digest}
    elif operation == "empty-source":
        source = next((s for s in recipe.sources if s.name == payload["source"]), None)
        if source is None:
            raise RecipeValidationError("unknown source")
        if any(_safe(root, source.relative_path).iterdir()):
            raise IntegrityError("refusing to initialize a nonempty unowned source directory")
        facts = {"empty": True}
    elif operation == "node-lock":
        ref = environment.node_lock
        if ref is None:
            raise RecipeValidationError("environment has no node lock")
        source = next(s for s in recipe.sources if s.name == ref.source)
        lock = _safe(root, source.relative_path, ref.path)
        if _hash(lock) != ref.sha256:
            raise IntegrityError("node lock bytes differ")
        nodes = tomllib.loads(lock.read_text())["nodepacks"]
        comfy = next(s for s in recipe.sources if s.name == "comfyui")
        for node in recipe.sources:
            prefix = comfy.relative_path + "/custom_nodes/"
            if node.relative_path.startswith(prefix):
                name = node.relative_path[len(prefix):]
                if nodes.get(name, {}).get("commit") != node.pin.revision:
                    raise IntegrityError("node lock commit differs from canonical source")
        facts = {"valid": True}
    elif operation == "lock-inputs":
        directory, lock = _paths(root, environment)
        if lock.exists():
            _validate_lock(recipe, digest, environment, root)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            meta = _safe(root, str(directory.relative_to(root)), "lock.json")
            temporary = _safe(root, str(directory.relative_to(root)), "requirements.lock.tmp")
            if meta.exists():
                # A prior commit persisted identity before promotion. Reuse
                # those exact bytes; do not invoke the resolver on retry.
                if (not temporary.is_file() or json.loads(meta.read_text())
                        != _lock_identity(recipe, digest, environment, temporary)):
                    raise IntegrityError("interrupted requirements lock identity differs")
                _entries(temporary.read_text())
                _atomic(lock, temporary.read_bytes())
                _validate_lock(recipe, digest, environment, root)
                return {"schema": _HELPER_SCHEMA, "operation": operation,
                        "facts": {"lock_exists": True}}
            req = environment.requirements
            if req.mode == "packaged":
                _, data = recipes._read_ref(req.packaged_lock)
                _atomic(_safe(root, str(directory.relative_to(root)), "requirements.lock.tmp"), data)
            else:
                lines = list(req.constraints)
                lines += [f"{w.name} @ {w.url}" for w in environment.final_wheels]
                for source_path in req.input_paths:
                    source = next(s for s in recipe.sources if s.name == source_path.source)
                    path = _safe(root, source.relative_path, source_path.path)
                    if not path.is_file():
                        raise IntegrityError("requirements input is absent")
                    if path.name == "pyproject.toml":
                        lines += tomllib.loads(path.read_text()).get("build-system", {}).get("requires", [])
                _atomic(_safe(root, str(directory.relative_to(root)), "requirements.in"),
                        ("\n".join(lines) + "\n").encode())
        facts = {"lock_exists": lock.exists()}
    elif operation == "commit-lock":
        directory, lock = _paths(root, environment)
        if lock.exists():
            _validate_lock(recipe, digest, environment, root)
        else:
            temporary = _safe(root, str(directory.relative_to(root)), "requirements.lock.tmp")
            payload_bytes = temporary.read_bytes()
            _entries(payload_bytes.decode())
            if environment.requirements.mode == "packaged" and hashlib.sha256(payload_bytes).hexdigest() != environment.requirements.packaged_lock.sha256:
                raise IntegrityError("packaged requirements lock mismatch")
            # Bind the candidate before promoting it. A crash after this write
            # leaves the unchanged candidate reusable, never an unbound lock.
            _atomic(_safe(root, str(directory.relative_to(root)), "lock.json"),
                    _json(_lock_identity(recipe, digest, environment, lock,
                                        digest=hashlib.sha256(payload_bytes).hexdigest())).encode())
            _atomic(lock, payload_bytes)
        facts = {"requirements_lock_sha256": _hash(lock)}
    elif operation == "install-locks":
        directory, lock = _validate_lock(recipe, digest, environment, root)
        entries = _entries(lock.read_text())
        names = {w.name.lower().replace("_", "-") for w in environment.final_wheels}
        support = [row[2] for row in entries
                   if re.match(r"[A-Za-z0-9._-]+", row[0])[0].lower().replace("_", "-") not in names]
        wheels = [f"{w.name} @ {w.url} --hash=sha256:{w.sha256}" for w in environment.final_wheels]
        _atomic(_safe(root, str(directory.relative_to(root)), "support.lock"), ("\n".join(support) + "\n").encode())
        _atomic(_safe(root, str(directory.relative_to(root)), "wheels.lock"), ("\n".join(wheels) + "\n").encode())
        facts = {"requirements_lock_sha256": _hash(lock)}
    elif operation == "environment-spec":
        facts = _environment_spec(recipe, environment, root, digest)
    else:  # JSON is also valid YAML, avoids unsafe YAML interpolation.
        configuration = {"astrid_engine": {"base_path": str(_safe(root, "models")),
                         **{name: name for name in ("diffusion_models", "text_encoders", "vae", "loras")}}}
        configuration_path = _safe(root, "runtime/extra_model_paths.yaml")
        configuration_bytes = _json(configuration).encode()
        if operation == "models-config":
            _atomic(configuration_path, configuration_bytes)
            facts = {"written": True}
        else:
            facts = {"valid": configuration_path.is_file()
                     and configuration_path.read_bytes() == configuration_bytes}
    return {"schema": _HELPER_SCHEMA, "operation": operation, "facts": facts}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 2 or len(argv[1]) > 65536:
            raise RecipeValidationError("expected bounded helper operation and JSON request")
        result = dispatch(argv[0], json.loads(argv[1]))
    except (EngineError, OSError, ValueError, KeyError, TypeError) as exc:
        # Context/runner retain bounded target diagnostics. Never echo attacker
        # controlled JSON, paths, command output or exception text.
        if len(argv) == 2 and argv[0] in _OPERATIONS:
            error_code = exc.code if isinstance(exc, EngineError) else "integrity_failed"
            print(_json({"schema": _HELPER_SCHEMA, "operation": argv[0],
                         "facts": {"error_code": error_code}}))
            return 0
        print("engine target helper validation failed", file=sys.stderr)
        return 1
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
