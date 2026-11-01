"""Strict local-only loaders for packaged engine recipes, pins and model locks."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .engine_contracts import (
    DataRef, EngineRecipe, EnvironmentSpec, HealthSpec, IntegrityError, ModelLock,
    ModelLockValidationError, ModelSpec, RecipeIndex, RecipeIndexEntry,
    RecipeValidationError, RequirementsSpec, SourceFileRef, SourceInstall,
    SourcePath, SourcePin, SourceSpec, StartSpec, UnsupportedPlatform,
    UnknownEngine, UnsafePathError, WheelSpec,
)

_ROOT = Path(__file__).resolve().parents[2]
_INDEX_PATH = _ROOT / "packs" / "engine-index.json"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ENGINE = re.compile(r"^[a-z][a-z0-9-]*$")
_PLATFORMS = {"linux-x86_64-cu124", "linux-x86_64-cu128", "linux-x86_64-cu130", "windows-x86_64-cu128", "windows-x86_64-cu130"}
_PLACEHOLDERS = {"install_root", "python", "comfy_root", "models_root", "models_config", "host", "port"}


def _object(value: Any, required: set[str], optional: set[str] | None = None, where: str = "object") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecipeValidationError(f"{where} must be an object")
    optional = set() if optional is None else optional
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing or extra:
        detail = []
        if missing: detail.append("missing " + ", ".join(sorted(missing)))
        if extra: detail.append("unknown " + ", ".join(sorted(extra)))
        raise RecipeValidationError(f"{where}: " + "; ".join(detail))
    return value


def _string(value: Any, where: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise RecipeValidationError(f"{where} must be a {'non-empty ' if nonempty else ''}string")
    return value


def _array(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise RecipeValidationError(f"{where} must be an array")
    return value


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RecipeValidationError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _relative(value: Any, where: str) -> str:
    raw = _string(value, where)
    path = PurePosixPath(raw)
    if ("\\" in raw or ":" in raw or path.is_absolute() or
            any(part in {"", ".", ".."} for part in raw.split("/"))):
        raise UnsafePathError(f"{where} must be a safe normalized POSIX relative path")
    return raw


def _https(value: Any, where: str) -> str:
    raw = _string(value, where)
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RecipeValidationError(f"{where} must be credential-free HTTPS")
    return raw


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecipeValidationError(f"packaged JSON is unreadable: {path.name}") from exc


def _resolve_ref(raw: Any, where: str) -> DataRef:
    obj = _object(raw, {"path", "sha256"}, where=where)
    return DataRef(_relative(obj["path"], where + ".path"), _digest(obj["sha256"], where + ".sha256"))


def _read_ref(ref: DataRef) -> tuple[Path, bytes]:
    path = _ROOT / ref.path
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(_ROOT.resolve()) or path.is_symlink() or not resolved.is_file():
            raise UnsafePathError(f"packaged data path escapes Astrid: {ref.path}")
        payload = resolved.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"packaged data is unavailable: {ref.path}") from exc
    if hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise IntegrityError(f"packaged data digest mismatch: {ref.path}")
    return resolved, payload


def _parse_pins(raw: Any) -> dict[str, SourcePin]:
    obj = _object(raw, {"schema", "sources"}, where="pins")
    if obj["schema"] != "astrid.engine-pins.v1" or not isinstance(obj["sources"], dict):
        raise RecipeValidationError("pins schema must be astrid.engine-pins.v1 with a source map")
    result = {}
    for name, value in obj["sources"].items():
        if not isinstance(name, str) or not name:
            raise RecipeValidationError("pin keys must be non-empty strings")
        pin = _object(value, {"repository", "revision"}, where=f"pins.sources.{name}")
        repository = _https(pin["repository"], f"pins.sources.{name}.repository")
        revision = _string(pin["revision"], f"pins.sources.{name}.revision")
        if not _REVISION.fullmatch(revision):
            raise RecipeValidationError(f"pins.sources.{name}.revision must be a full lowercase Git commit")
        result[name] = SourcePin(repository, revision)
    return result


def load_pin_set(ref: DataRef) -> Mapping[str, SourcePin]:
    """Load a hash-verified packaged pin set for compatibility adapters."""
    if not isinstance(ref, DataRef):
        raise RecipeValidationError("load_pin_set requires a DataRef")
    _, payload = _read_ref(ref)
    try:
        parsed = _parse_pins(json.loads(payload))
    except json.JSONDecodeError as exc:
        raise RecipeValidationError("pins are invalid JSON") from exc
    return MappingProxyType(parsed)


def _placeholder_check(values: list[str], where: str) -> None:
    for value in values:
        if not isinstance(value, str):
            raise RecipeValidationError(f"{where} values must be strings")
        for match in re.findall(r"\{([^{}]+)\}", value):
            if match not in _PLACEHOLDERS:
                raise RecipeValidationError(f"unknown recipe placeholder {{{match}}} in {where}")
        if "{" in re.sub(r"\{[^{}]+\}", "", value) or "}" in re.sub(r"\{[^{}]+\}", "", value):
            raise RecipeValidationError(f"malformed recipe placeholder in {where}")


def _load_recipe(ref: DataRef, index_engine: str, index_platform: str) -> EngineRecipe:
    _, payload = _read_ref(ref)
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RecipeValidationError("recipe is invalid JSON") from exc
    top = _object(raw, {"schema", "engine", "platform", "pins", "source_bindings", "environments", "primary_environment", "models_lock", "start", "health"}, where="recipe")
    if top["schema"] != "astrid.engine-recipe.v1": raise RecipeValidationError("unknown recipe schema")
    engine = _string(top["engine"], "recipe.engine")
    platform = _string(top["platform"], "recipe.platform")
    if not _ENGINE.fullmatch(engine): raise RecipeValidationError("recipe.engine has invalid syntax")
    if platform not in _PLATFORMS: raise RecipeValidationError("recipe.platform is unsupported")
    if engine != index_engine or platform != index_platform: raise RecipeValidationError("recipe identity differs from index entry")
    pins_ref = _resolve_ref(top["pins"], "recipe.pins")
    _, pins_raw = _read_ref(pins_ref)
    try: pins = _parse_pins(json.loads(pins_raw))
    except json.JSONDecodeError as exc: raise RecipeValidationError("pins are invalid JSON") from exc
    bindings = _array(top["source_bindings"], "recipe.source_bindings")
    sources, names, source_paths = [], set(), set()
    for i, binding_raw in enumerate(bindings):
        binding = _object(binding_raw, {"name", "pin", "relative_path"}, where=f"recipe.source_bindings[{i}]")
        name = _string(binding["name"], "source.name")
        key = _string(binding["pin"], "source.pin")
        if name in names: raise RecipeValidationError(f"duplicate source name: {name}")
        if key not in pins: raise RecipeValidationError(f"source references unknown pin: {key}")
        relative_path = _relative(binding["relative_path"], "source.relative_path")
        if relative_path in source_paths: raise RecipeValidationError("duplicate source path")
        names.add(name)
        source_paths.add(relative_path)
        sources.append(SourceSpec(name, pins[key], relative_path))
    if not sources: raise RecipeValidationError("recipe must bind at least one source")
    environments = []
    env_names = set()
    for i, env_raw in enumerate(_array(top["environments"], "recipe.environments")):
        where = f"recipe.environments[{i}]"
        env = _object(env_raw, {"name", "relative_path", "python_version", "uv_version", "requirements", "final_wheels", "node_lock", "source_installs"}, where=where)
        name = _string(env["name"], where + ".name")
        if name in env_names: raise RecipeValidationError(f"duplicate environment name: {name}")
        env_names.add(name)
        req = _object(env["requirements"], {"mode", "packaged_lock", "input_paths", "constraints", "exclude_newer"}, where=where + ".requirements")
        mode = req["mode"]
        if mode not in {"packaged", "resolve-once"}: raise RecipeValidationError("unknown requirements mode")
        lock = _resolve_ref(req["packaged_lock"], where + ".packaged_lock") if req["packaged_lock"] is not None else None
        inputs, input_keys = [], set()
        for j, item in enumerate(_array(req["input_paths"], where + ".input_paths")):
            row = _object(item, {"source", "path"}, where=f"{where}.input_paths[{j}]")
            if row["source"] not in names: raise RecipeValidationError("requirements input references unknown source")
            input_path = _relative(row["path"], "requirements input path")
            input_key = (row["source"], input_path)
            if input_key in input_keys: raise RecipeValidationError("duplicate requirements source path")
            input_keys.add(input_key)
            inputs.append(SourcePath(row["source"], input_path))
        constraints = tuple(_string(x, "requirements constraint") for x in _array(req["constraints"], where + ".constraints"))
        cutoff = req["exclude_newer"]
        if cutoff is not None:
            try:
                parsed = datetime.fromisoformat(_string(cutoff, "exclude_newer").replace("Z", "+00:00"))
                if parsed.utcoffset() is None: raise ValueError
            except ValueError as exc: raise RecipeValidationError("exclude_newer must be timezone-aware ISO8601") from exc
        if (mode == "packaged" and (lock is None or inputs or constraints or cutoff is not None)) or (mode == "resolve-once" and (lock is not None or not inputs or cutoff is None)):
            raise RecipeValidationError("requirements mode fields are inconsistent")
        wheels = []
        for j, wheel_raw in enumerate(_array(env["final_wheels"], where + ".final_wheels")):
            w = _object(wheel_raw, {"name", "version", "url", "sha256"}, where=f"{where}.final_wheels[{j}]")
            wheels.append(WheelSpec(_string(w["name"], "wheel.name"), _string(w["version"], "wheel.version"), _https(w["url"], "wheel.url"), _digest(w["sha256"], "wheel.sha256")))
        node_lock = None
        if env["node_lock"] is not None:
            nl = _object(env["node_lock"], {"source", "path", "sha256"}, where=where + ".node_lock")
            if nl["source"] not in names: raise RecipeValidationError("node_lock references unknown source")
            node_lock = SourceFileRef(nl["source"], _relative(nl["path"], "node_lock.path"), _digest(nl["sha256"], "node_lock.sha256"))
        installs = []
        for j, ins_raw in enumerate(_array(env["source_installs"], where + ".source_installs")):
            ins = _object(ins_raw, {"source", "argv", "check_modules"}, where=f"{where}.source_installs[{j}]")
            if ins["source"] not in names: raise RecipeValidationError("source install references unknown source")
            argv = tuple(_string(x, "source install argv") for x in _array(ins["argv"], "source install argv"))
            _placeholder_check(list(argv), "source install argv")
            installs.append(SourceInstall(ins["source"], argv, tuple(_string(x, "check module") for x in _array(ins["check_modules"], "check_modules"))))
        environments.append(EnvironmentSpec(name, _relative(env["relative_path"], "environment.relative_path"), _string(env["python_version"], "python_version"), _string(env["uv_version"], "uv_version"), RequirementsSpec(mode, lock, tuple(inputs), constraints, cutoff), tuple(wheels), node_lock, tuple(installs)))
    primary = _string(top["primary_environment"], "primary_environment")
    if primary not in env_names: raise RecipeValidationError("primary_environment does not name an environment")
    models_ref = _resolve_ref(top["models_lock"], "recipe.models_lock")
    start_raw = _object(top["start"], {"argv", "cwd", "env", "bind_host", "port"}, where="recipe.start")
    argv = tuple(_string(x, "start.argv") for x in _array(start_raw["argv"], "start.argv"))
    if not argv: raise RecipeValidationError("start.argv must not be empty")
    start_env = _object(start_raw["env"], set(start_raw["env"].keys()) if isinstance(start_raw["env"], dict) else set(), where="start.env")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in start_env.items()): raise RecipeValidationError("start.env must map strings to strings")
    cwd = _string(start_raw["cwd"], "start.cwd")
    _placeholder_check([*argv, cwd, *start_env.values()], "start")
    if "{" not in cwd:
        cwd = _relative(cwd, "start.cwd")
    port = start_raw["port"]
    if type(port) is not int or not 1 <= port <= 65535: raise RecipeValidationError("start.port must be in 1..65535")
    if start_raw["bind_host"] != "127.0.0.1": raise RecipeValidationError("start.bind_host must be loopback")
    health_raw = _object(top["health"], {"path", "object_info_path", "required_nodes", "timeout_seconds", "poll_interval_seconds"}, where="recipe.health")
    nodes = tuple(_string(x, "required node") for x in _array(health_raw["required_nodes"], "health.required_nodes"))
    for field in ("path", "object_info_path"):
        value = health_raw[field]
        parsed_path = urlsplit(value) if isinstance(value, str) else None
        if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or
                "\\" in value or "?" in value or "#" in value or
                parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment or
                ".." in value.split("/")):
            raise RecipeValidationError(f"health.{field} must be a safe same-origin absolute URL path")
    timeout, interval = health_raw["timeout_seconds"], health_raw["poll_interval_seconds"]
    try:
        finite_timeout = type(timeout) in {int, float} and math.isfinite(timeout)
        finite_interval = type(interval) in {int, float} and math.isfinite(interval)
    except OverflowError:
        finite_timeout = finite_interval = False
    if (not finite_timeout or not finite_interval or
            timeout <= 0 or interval <= 0 or interval > timeout):
        raise RecipeValidationError("health timeouts must be positive and poll interval cannot exceed timeout")
    health = HealthSpec(health_raw["path"], health_raw["object_info_path"], nodes, float(timeout), float(interval))
    return EngineRecipe(top["schema"], engine, platform, pins_ref, tuple(sources), tuple(environments), primary, models_ref, StartSpec(argv, cwd, MappingProxyType(dict(start_env)), "127.0.0.1", port), health)


def _load_index() -> RecipeIndex:
    raw = _json(_INDEX_PATH)
    obj = _object(raw, {"schema", "entries"}, where="engine-index")
    if obj["schema"] != "astrid.engine-index.v1": raise RecipeValidationError("unknown recipe index schema")
    entries, seen = [], set()
    prior = None
    for i, item_raw in enumerate(_array(obj["entries"], "engine-index.entries")):
        item = _object(item_raw, {"engine", "platform", "recipe"}, where=f"engine-index.entries[{i}]")
        engine, platform = _string(item["engine"], "index.engine"), _string(item["platform"], "index.platform")
        if not _ENGINE.fullmatch(engine) or platform not in _PLATFORMS: raise RecipeValidationError("invalid index engine/platform")
        key = (engine, platform)
        if key in seen: raise RecipeValidationError("duplicate engine/platform index entry")
        if prior is not None and key < prior: raise RecipeValidationError("index entries must be deterministically sorted")
        seen.add(key); prior = key
        entries.append(RecipeIndexEntry(engine, platform, _resolve_ref(item["recipe"], "index.recipe")))
    return RecipeIndex(obj["schema"], tuple(entries))


def load_recipe_index() -> RecipeIndex:
    return _load_index()


def load_recipe(engine: str, platform: str) -> EngineRecipe:
    if not isinstance(engine, str) or not _ENGINE.fullmatch(engine): raise UnknownEngine(engine)
    index = _load_index()
    matches = [entry for entry in index.entries if entry.engine == engine]
    if not matches:
        # A pack-owned canonical pin set establishes a known engine even when
        # CONTRACTS v1 has no PlatformKey for one of its existing recipes.
        if (_ROOT / "packs" / engine / "pins.json").is_file():
            raise UnsupportedPlatform(f"{engine} has no indexed recipe for {platform}")
        raise UnknownEngine(engine)
    if platform not in _PLATFORMS or not any(entry.platform == platform for entry in matches): raise UnsupportedPlatform(f"{engine} does not support {platform}")
    entry = next(entry for entry in matches if entry.platform == platform)
    return _load_recipe(entry.recipe, engine, platform)


def _load_model_lock_ref(ref: DataRef) -> ModelLock:
    _, payload = _read_ref(ref)
    try: raw = json.loads(payload)
    except json.JSONDecodeError as exc: raise ModelLockValidationError("model lock is invalid JSON") from exc
    top = _object(raw, {"schema", "models"}, where="model lock")
    if top["schema"] != "astrid.models-lock.v1": raise ModelLockValidationError("unknown model lock schema")
    models, names, destinations = [], set(), set()
    for i, row_raw in enumerate(_array(top["models"], "model lock.models")):
        row = _object(row_raw, {"name", "repository", "revision", "path", "url", "destination", "bytes", "sha256"}, where=f"models[{i}]")
        name = _string(row["name"], "model.name")
        if name in names: raise ModelLockValidationError("duplicate model name")
        names.add(name)
        repo, revision, source_path, url = row["repository"], row["revision"], row["path"], row["url"]
        hf = all(value is not None for value in (repo, revision, source_path)) and url is None
        web = repo is None and revision is None and source_path is None and url is not None
        if not (hf ^ web): raise ModelLockValidationError("model must have exactly one valid source form")
        if hf:
            _string(repo, "model.repository")
            if not _REVISION.fullmatch(_string(revision, "model.revision")): raise ModelLockValidationError("model.revision must be an immutable lowercase commit")
            source_path = _relative(source_path, "model.path")
        else: url = _https(url, "model.url")
        destination = _relative(row["destination"], "model.destination")
        if destination in destinations: raise ModelLockValidationError("duplicate model destination")
        destinations.add(destination)
        size = row["bytes"]
        if type(size) is not int or size <= 0: raise ModelLockValidationError("model.bytes must be a positive integer")
        try: digest = _digest(row["sha256"], "model.sha256")
        except RecipeValidationError as exc: raise ModelLockValidationError(str(exc)) from exc
        models.append(ModelSpec(name, repo, revision, source_path, url, destination, size, digest))
    return ModelLock(top["schema"], tuple(models))


def load_model_lock(ref: DataRef) -> ModelLock:
    if not isinstance(ref, DataRef): raise ModelLockValidationError("load_model_lock requires a DataRef")
    try:
        return _load_model_lock_ref(ref)
    except (ModelLockValidationError, UnsafePathError):
        raise
    except RecipeValidationError as exc:
        raise ModelLockValidationError(str(exc)) from exc


def load_recipe_model_lock(engine: str, platform: str) -> ModelLock:
    recipe = load_recipe(engine, platform)
    return load_model_lock(recipe.models_lock)


def engine_source_revision(engine: str, platform: str, source: str) -> str:
    recipe = load_recipe(engine, platform)
    for item in recipe.sources:
        if item.name == source:
            return item.pin.revision
    raise RecipeValidationError(f"{engine} has no pinned source named {source}")


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def serialize_recipe(recipe: EngineRecipe) -> str:
    if not isinstance(recipe, EngineRecipe):
        raise RecipeValidationError("serialize_recipe requires an EngineRecipe")
    return json.dumps(_plain(recipe), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def serialize_model_lock(lock: ModelLock) -> str:
    if not isinstance(lock, ModelLock):
        raise ModelLockValidationError("serialize_model_lock requires a ModelLock")
    return json.dumps(_plain(lock), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def recipe_digest(recipe: EngineRecipe) -> str:
    payload = serialize_recipe(recipe).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["load_recipe_index", "load_recipe", "load_model_lock", "load_recipe_model_lock", "load_pin_set",
           "engine_source_revision", "serialize_recipe", "serialize_model_lock", "recipe_digest"]
