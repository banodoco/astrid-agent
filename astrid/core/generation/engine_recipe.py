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
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

from .engine_contracts import (
    DataRef, EngineRecipe, EnvironmentSpec, HealthSpec, IntegrityError, ModelLock,
    ModelLockValidationError, ModelSpec, RecipeIndex, RecipeIndexEntry,
    RecipeValidationError, RequirementsSpec, SourceFileRef, SourceInstall,
    SourcePath, SourcePin, SourceSpec, StartSpec, UnsupportedPlatform,
    UnknownEngine, UnsafePathError, WheelSpec,
)

if TYPE_CHECKING:
    from astrid.core.pack.canonical import BundledCatalog, ResourceHandle


_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_POINT = "extensions/generation.engine"
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


def _check_ref_selection(ref: DataRef, allowed: Mapping[str, ResourceHandle] | None) -> None:
    if allowed is not None:
        if ref.path not in allowed:
            raise RecipeValidationError(f"resource is not declared by the selected pack catalog: {ref.path}")
        if ref.sha256 != allowed[ref.path].sha256:
            raise IntegrityError(f"selected pack resource digest differs from declaration: {ref.path}")


def _read_ref(ref: DataRef, *, allowed: Mapping[str, ResourceHandle] | None = None) -> tuple[Path, bytes]:
    _check_ref_selection(ref, allowed)
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
    obj = _object(raw, {"schema", "sources"}, {"includes"}, where="pins")
    if obj["schema"] != "astrid.engine-pins.v1" or not isinstance(obj["sources"], dict):
        raise RecipeValidationError("pins schema must be astrid.engine-pins.v1 with a source map")
    result = {}
    for name, value in obj["sources"].items():
        if not isinstance(name, str) or not name:
            raise RecipeValidationError("pin keys must be non-empty strings")
        pin = _object(value, {"repository", "revision"}, {"version"}, where=f"pins.sources.{name}")
        if "version" in pin and (not isinstance(pin["version"], str) or not pin["version"].strip()):
            raise RecipeValidationError(f"pins.sources.{name}.version must be a non-empty string")
        repository = _https(pin["repository"], f"pins.sources.{name}.repository")
        revision = _string(pin["revision"], f"pins.sources.{name}.revision")
        if not _REVISION.fullmatch(revision):
            raise RecipeValidationError(f"pins.sources.{name}.revision must be a full lowercase Git commit")
        result[name] = SourcePin(repository, revision)
    return result


def _selected_resources(registry: BundledCatalog | None) -> Mapping[str, ResourceHandle] | None:
    """Constrain refs to an explicitly admitted canonical catalog, without discovery."""
    if registry is None:
        return None
    selected: dict[str, ResourceHandle] = {}
    for entry in registry.ordered_entries:
        try:
            manifest_path = entry.manifest.resolved.relative_to(_ROOT).as_posix()
        except ValueError as exc:
            raise UnsafePathError("selected engine catalog must belong to this Astrid package") from exc
        _read_ref(DataRef(manifest_path, entry.manifest.sha256))
        for handle in entry.resources:
            if not handle.resolved.is_relative_to(entry.root):
                raise UnsafePathError(f"selected resource escapes its owner: {handle.path}")
            try:
                relative = handle.resolved.relative_to(_ROOT).as_posix()
            except ValueError as exc:
                raise UnsafePathError(f"selected resource is outside this Astrid package: {handle.path}") from exc
            if relative in selected and selected[relative].sha256 != handle.sha256:
                raise IntegrityError(f"conflicting selected resource digests: {relative}")
            selected[relative] = handle
    return MappingProxyType(selected)


def _load_pins(ref: DataRef, *, allowed: Mapping[str, ResourceHandle] | None = None,
               active: tuple[str, ...] = ()) -> tuple[dict[str, SourcePin], tuple[DataRef, ...]]:
    if ref.path in active:
        raise RecipeValidationError(f"cyclic pin set reference: {ref.path}")
    _, payload = _read_ref(ref, allowed=allowed)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecipeValidationError(f"pins are invalid JSON: {ref.path}") from exc
    parsed = _parse_pins(raw)
    refs = [ref]
    for item in _array(raw.get("includes", []), "pins.includes"):
        included = _resolve_ref(item, "pins.includes")
        imported, imported_refs = _load_pins(included, allowed=allowed, active=(*active, ref.path))
        overlap = parsed.keys() & imported.keys()
        if overlap:
            raise RecipeValidationError("duplicate pin authority: " + ", ".join(sorted(overlap)))
        parsed.update(imported)
        refs.extend(imported_refs)
    return parsed, tuple(refs)


def load_pin_set(ref: DataRef, *, registry: BundledCatalog | None = None) -> Mapping[str, SourcePin]:
    """Load only the explicitly hash-bound pin graph; never discover sibling pins."""
    if not isinstance(ref, DataRef):
        raise RecipeValidationError("load_pin_set requires a DataRef")
    parsed, _ = _load_pins(ref, allowed=_selected_resources(registry))
    return MappingProxyType(parsed)


def pin_set_refs(ref: DataRef) -> tuple[DataRef, ...]:
    """Verified transitive pin closure for the existing target helper identity."""
    if not isinstance(ref, DataRef):
        raise RecipeValidationError("pin_set_refs requires a DataRef")
    _, refs = _load_pins(ref)
    return refs


def _placeholder_check(values: list[str], where: str) -> None:
    for value in values:
        if not isinstance(value, str):
            raise RecipeValidationError(f"{where} values must be strings")
        for match in re.findall(r"\{([^{}]+)\}", value):
            if match not in _PLACEHOLDERS:
                raise RecipeValidationError(f"unknown recipe placeholder {{{match}}} in {where}")
        if "{" in re.sub(r"\{[^{}]+\}", "", value) or "}" in re.sub(r"\{[^{}]+\}", "", value):
            raise RecipeValidationError(f"malformed recipe placeholder in {where}")


def _load_recipe(ref: DataRef, index_engine: str, index_platform: str, *,
                 allowed: Mapping[str, ResourceHandle] | None = None) -> EngineRecipe:
    _, payload = _read_ref(ref, allowed=allowed)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecipeValidationError(f"recipe is invalid JSON: {ref.path}") from exc
    top = _object(raw, {"schema", "engine", "platform", "pins", "source_bindings", "environments", "primary_environment", "models_lock", "start", "health"}, where="recipe")
    if top["schema"] != "astrid.engine-recipe.v1": raise RecipeValidationError("unknown recipe schema")
    engine = _string(top["engine"], "recipe.engine")
    platform = _string(top["platform"], "recipe.platform")
    if not _ENGINE.fullmatch(engine): raise RecipeValidationError("recipe.engine has invalid syntax")
    if platform not in _PLATFORMS: raise RecipeValidationError("recipe.platform is unsupported")
    if engine != index_engine or platform != index_platform: raise RecipeValidationError("recipe identity differs from index entry")
    pins_ref = _resolve_ref(top["pins"], "recipe.pins")
    pins, _ = _load_pins(pins_ref, allowed=allowed)
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
        if lock is not None:
            _check_ref_selection(lock, allowed)
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
    _check_ref_selection(models_ref, allowed)
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


def declaration_paths() -> list[str]:
    """Package-relative paths of the generation.engine contributions that declare recipe rows."""
    return [m.relative_to(_ROOT).as_posix() for m in sorted((_ROOT / "packs").glob(f"*/{_ENGINE_POINT}/*.yaml"))]


def parse_recipe_declarations(text: str, where: str = "contribution") -> list[dict[str, str]]:
    """The ``recipes:`` rows of a generation.engine manifest: block-style ``platform``/``path``/``sha256``.

    A fixed-shape reader, so the helper closure needs no YAML library on a fresh release interpreter.
    """
    rows: list[dict[str, str]] = []
    inside = False
    for line in text.splitlines():
        if re.fullmatch(r"recipes:\s*", line):
            inside = True
        elif inside and line.strip() and not line[0].isspace():
            inside = False
        elif inside and line.strip():
            match = re.fullmatch(r"\s+(- )?(\w+): \"?([^\"\s]+)\"?\s*", line)
            if match is None: raise RecipeValidationError(f"{where}: unreadable recipes line")
            dash, key, value = match.groups()
            if dash: rows.append({})
            if not rows: raise RecipeValidationError(f"{where}: recipes row must start with '- '")
            if key not in {"platform", "path", "sha256"}: raise RecipeValidationError(f"{where}: unknown {key}")
            rows[-1][key] = value
    for row in rows:
        if row.keys() != {"platform", "path", "sha256"}: raise RecipeValidationError(f"{where}: recipes row needs platform, path, sha256")
    return rows


def _declared_rows() -> list[dict[str, Any]]:
    """Recipe rows declared by the generation.engine contributions (``recipes:`` in each manifest)."""
    rows = []
    for manifest in sorted((_ROOT / "packs").glob(f"*/{_ENGINE_POINT}/*.yaml")):
        try:
            text = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RecipeValidationError(f"engine contribution is unreadable: {manifest.name}") from exc
        for row in parse_recipe_declarations(text, manifest.name):
            rows.append({"engine": manifest.stem, "platform": row["platform"],
                         "recipe": {"path": row["path"], "sha256": row["sha256"]}})
    return rows


def _index_rows() -> list[dict[str, Any]]:
    rows, seen = [], set()
    for item in sorted(_declared_rows(), key=lambda r: (r["engine"], r["platform"])):
        engine, platform = _string(item["engine"], "index.engine"), _string(item["platform"], "index.platform")
        if not _ENGINE.fullmatch(engine) or platform not in _PLATFORMS:
            raise RecipeValidationError("invalid index engine/platform")
        if platform in {p for _, p in seen}:
            raise RecipeValidationError("more than one engine row for a platform")
        seen.add((engine, platform))
        rows.append(item)
    return rows


def _selected_row(item: Mapping[str, Any], allowed: Mapping[str, ResourceHandle] | None) -> bool:
    if allowed is None:
        return True
    ref = item["recipe"]
    path = ref.get("path") if isinstance(ref, dict) else None
    return isinstance(path, str) and path in allowed and allowed[path].kind == "resource:engine-recipe"


def _load_index(*, allowed: Mapping[str, ResourceHandle] | None = None) -> RecipeIndex:
    entries = tuple(
        RecipeIndexEntry(item["engine"], item["platform"], _resolve_ref(item["recipe"], "index.recipe"))
        for item in _index_rows() if _selected_row(item, allowed)
    )
    return RecipeIndex("astrid.engine-index.v1", entries)


def load_recipe_index(*, registry: BundledCatalog | None = None) -> RecipeIndex:
    """Project the shipped registry over explicitly selected canonical resources."""
    return _load_index(allowed=_selected_resources(registry))


def load_recipe(engine: str, platform: str, *, registry: BundledCatalog | None = None) -> EngineRecipe:
    """Select once by registered identity, optionally confined to an admitted catalog.

    The shipped index is metadata, not filesystem discovery. Only the selected
    recipe and its explicit pin imports are opened. A supplied catalog cannot
    fall back to a same-ID recipe in an unselected pack.
    """
    if not isinstance(engine, str) or not _ENGINE.fullmatch(engine):
        raise UnknownEngine(engine)
    allowed = _selected_resources(registry)
    matches = [item for item in _index_rows() if item["engine"] == engine and _selected_row(item, allowed)]
    if not matches:
        raise UnknownEngine(f"engine is not registered in the selected catalog: {engine}")
    if platform not in _PLATFORMS or not any(item["platform"] == platform for item in matches):
        raise UnsupportedPlatform(f"{engine} does not support {platform}")
    entry = next(item for item in matches if item["platform"] == platform)
    return _load_recipe(_resolve_ref(entry["recipe"], "index.recipe"), engine, platform, allowed=allowed)


def _load_model_lock_ref(ref: DataRef, *, allowed: Mapping[str, ResourceHandle] | None = None) -> ModelLock:
    _, payload = _read_ref(ref, allowed=allowed)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelLockValidationError(f"model lock is invalid JSON: {ref.path}") from exc
    top = _object(raw, {"schema", "models"}, where="model lock")
    if top["schema"] != "astrid.models-lock.v1":
        raise ModelLockValidationError("unknown model lock schema")
    models, names, destinations = [], set(), set()
    for i, row_raw in enumerate(_array(top["models"], "model lock.models")):
        row = _object(row_raw, {"name", "repository", "revision", "path", "url", "destination", "bytes", "sha256"}, where=f"models[{i}]")
        name = _string(row["name"], "model.name")
        if name in names:
            raise ModelLockValidationError("duplicate model name")
        names.add(name)
        repo, revision, source_path, url = row["repository"], row["revision"], row["path"], row["url"]
        hf = all(value is not None for value in (repo, revision, source_path)) and url is None
        web = repo is None and revision is None and source_path is None and url is not None
        if not (hf ^ web):
            raise ModelLockValidationError("model must have exactly one valid source form")
        if hf:
            _string(repo, "model.repository")
            if not _REVISION.fullmatch(_string(revision, "model.revision")):
                raise ModelLockValidationError("model.revision must be an immutable lowercase commit")
            source_path = _relative(source_path, "model.path")
        else:
            url = _https(url, "model.url")
        destination = _relative(row["destination"], "model.destination")
        if destination in destinations:
            raise ModelLockValidationError("duplicate model destination")
        destinations.add(destination)
        size = row["bytes"]
        if type(size) is not int or size <= 0:
            raise ModelLockValidationError("model.bytes must be a positive integer")
        try:
            digest = _digest(row["sha256"], "model.sha256")
        except RecipeValidationError as exc:
            raise ModelLockValidationError(str(exc)) from exc
        models.append(ModelSpec(name, repo, revision, source_path, url, destination, size, digest))
    return ModelLock(top["schema"], tuple(models))


def load_model_lock(ref: DataRef, *, registry: BundledCatalog | None = None) -> ModelLock:
    if not isinstance(ref, DataRef):
        raise ModelLockValidationError("load_model_lock requires a DataRef")
    try:
        return _load_model_lock_ref(ref, allowed=_selected_resources(registry))
    except (ModelLockValidationError, UnsafePathError):
        raise
    except RecipeValidationError as exc:
        raise ModelLockValidationError(str(exc)) from exc


def load_recipe_model_lock(engine: str, platform: str, *, registry: BundledCatalog | None = None) -> ModelLock:
    recipe = load_recipe(engine, platform, registry=registry)
    return load_model_lock(recipe.models_lock, registry=registry)


def engine_source_revision(engine: str, platform: str, source: str, *, registry: BundledCatalog | None = None) -> str:
    recipe = load_recipe(engine, platform, registry=registry)
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


__all__ = ["load_recipe_index", "load_recipe", "load_model_lock", "load_recipe_model_lock", "load_pin_set", "pin_set_refs",
           "engine_source_revision", "serialize_recipe", "serialize_model_lock", "recipe_digest"]
