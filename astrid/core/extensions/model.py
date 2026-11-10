"""The extension data model: points, contributions, effects, and manifest parsing.

Pure: no discovery, and no import of contribution code. Every manifest error is a
one-line ManifestError: ``<path>: <field>: <what is wrong>; <what to write>``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from jsonschema.exceptions import best_match
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from .refs import Ref, one_line, parse_ref, resolve_in_pack


class ManifestError(ValueError):
    """One line: ``<path>: <field>: <what is wrong>; <what to write>``."""


TOUCHES = (
    "timing",
    "params",
    "assets",
    "structure",
    "stacking",
    "narration",
    "notes",
    "intent",
    "chapters",
    "audio",
)
IO_ALLOWED = ("media.read",)
LEVELS = ("none", "read", "edit")
SCOPES = ("built-in", "installed", "user", "project")
ENVELOPE_KEYS = (
    "api",
    "name",
    "summary",
    "example",
    "entry",
    "version",
    "status",
    "deprecated",
    "budget",
    "effects",
    "compatibility",
)
KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
_SUMMARY_MAX = 120


@dataclass(frozen=True)
class Effects:
    level: Literal["none", "read", "edit"]
    touches: tuple[str, ...] = ()  # only for edit
    io: tuple[str, ...] = ()  # v1: only "media.read" may appear (and only where a point allows it)


@dataclass(frozen=True)
class Deprecation:
    since: str
    use: str | None
    remove_after: str | None


@dataclass(frozen=True)
class ExtensionPoint:
    id: str  # "<owner>.<name>"
    owner: str
    name: str
    description: str
    version: int
    min_supported: int  # loadable iff min_supported <= api <= version
    side: Literal["client", "agent"]
    cardinality: Literal["many", "one"]
    effects_allowed: frozenset[str]  # subset of {"none", "read", "edit"}
    io_allowed: frozenset[str]  # subset of {"media.read"}
    schema: Mapping[
        str, Any
    ]  # JSON Schema for point-specific contribution fields ({} = none allowed)
    protocol: Ref | None  # None = a data-only point ("protocol: data")
    harness: Ref | None
    docs: Ref | None
    template: Path | None
    path: Path | None  # its manifest


@dataclass(frozen=True)
class Contribution:
    key: str
    pack: str
    point: str
    version: str  # the contribution's version (default: the pack's version)
    digest: str  # sha256(manifest bytes)[:12]
    scope: Literal["built-in", "installed", "user", "project"]
    api: int
    compatibility: SpecifierSet | None
    effects: Effects
    entry: Ref | None  # None = data-only
    fields: Mapping[str, Any]  # every key that is not an envelope key
    summary: str
    example: str
    status: Literal["active", "experimental", "deprecated"]
    deprecated: Deprecation | None
    path: Path

    @property
    def id(self) -> str:  # "<pack>.<key>"
        return f"{self.pack}.{self.key}"

    @property
    def ref(self) -> str:  # "<pack>.<key>@<version>#<digest>"
        return f"{self.id}@{self.version}#{self.digest}"


@dataclass(frozen=True)
class Quarantined:
    id: str
    point: str | None
    path: Path
    error: str
    fix: str


# ---------------------------------------------------------------- helpers


def _fail(path: object, field: str, what: str, fix: str) -> ManifestError:
    return ManifestError(f"{path}: {field}: {one_line(what)}; {one_line(fix)}")


def _bad(where: str, what: str, fix: str) -> ManifestError:
    return ManifestError(f"{where}: {one_line(what)}; {one_line(fix)}")


def _dates_to_str(value: Any) -> Any:
    """YAML reads 2027-01-01 as a date; manifests mean the text."""
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: _dates_to_str(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dates_to_str(v) for v in value]
    return value


def _load_yaml(path: Path, text: str | None) -> dict[str, Any]:
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _fail(
                path, "file", f"cannot be read ({exc})", "make it a readable UTF-8 YAML file"
            ) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _fail(
            path, "yaml", f"is not valid YAML ({exc})", "fix the syntax at the reported line"
        ) from None
    if not isinstance(data, Mapping):
        raise _fail(
            path,
            "manifest",
            "must be a YAML mapping of key: value lines",
            "write the fields as key: value",
        )
    return _dates_to_str(dict(data))


def _schema_check(
    path: Path, data: Mapping[str, Any], schema: Mapping[str, Any], *, default_field: str, fix: str
) -> None:
    error = best_match(jsonschema.Draft202012Validator(schema).iter_errors(data))
    if error is None:
        return
    field = ".".join(str(p) for p in error.absolute_path) or default_field
    message = error.message
    match = re.search(
        r"'([^']+)' (?:was|were) unexpected|'([^']+)' is a required property", message
    )
    if match:
        field = match.group(1) or match.group(2)
    raise _fail(path, field, message, fix)


@lru_cache(maxsize=None)
def _schema_file(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _str_list(value: Any, *, where: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad(where, f"{field} must be a list of names", f"write {field}: [name, ...]")
    return list(value)


def _pack_root(path: Path) -> Path:
    """<pack>/extensions/points/<name>.yaml -> <pack>."""
    return path.parents[2] if len(path.parents) >= 3 else path.parent


def _ref(
    text: Any, *, path: Path, field: str, base_dir: Path, pack_root: Path, allow_module: bool = True
) -> Ref:
    try:
        return parse_ref(text, base_dir=base_dir, pack_root=pack_root, allow_module=allow_module)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {field}: {exc}") from None


def _check_schema_object(schema: Any, *, path: Path, field: str) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise _fail(
            path,
            field,
            "must be a JSON Schema object",
            "write a mapping, or a path to a .json file",
        )
    try:
        jsonschema.Draft202012Validator.check_schema(dict(schema))
    except jsonschema.SchemaError as exc:
        raise _fail(
            path, field, f"is not a valid JSON Schema ({exc.message})", "fix the schema"
        ) from None
    return dict(schema)


# ---------------------------------------------------------------- effects


def parse_effects(value: Any, *, where: str) -> Effects:
    """Short forms "none" | "read" | "edit"; long form {mutates, touches, io}.

    ``{mutates: false}`` is level "read" (``none`` only when written as "none").
    ``{mutates: true}`` is level "edit". ``touches`` only with mutates true.
    """
    if isinstance(value, str):
        if value in LEVELS:
            return Effects(level=value)  # type: ignore[arg-type]
        raise _bad(
            where,
            f"{value!r} is not an effects level",
            "write none, read or edit, or the long form {mutates, touches, io}",
        )
    if not isinstance(value, Mapping):
        raise _bad(
            where,
            f"is a {type(value).__name__}, not a level or a mapping",
            "write none, read or edit, or {mutates: bool, touches: [...], io: [...]}",
        )
    unknown = sorted(set(value) - {"mutates", "touches", "io"})
    if unknown:
        raise _bad(where, f"unknown key {unknown[0]!r}", "use only mutates, touches and io")
    mutates = value.get("mutates")
    if not isinstance(mutates, bool):
        raise _bad(where, "mutates must be true or false", "write mutates: true or mutates: false")
    touches = _str_list(value.get("touches"), where=where, field="touches")
    io = _str_list(value.get("io"), where=where, field="io")
    if touches and not mutates:
        raise _bad(
            where,
            "touches is only allowed when mutates is true",
            "remove touches, or set mutates: true",
        )
    for category in touches:
        if category not in TOUCHES:
            raise _bad(
                where,
                f"touches has {category!r}, which is not a touch category",
                f"use only: {', '.join(TOUCHES)}",
            )
    for capability in io:
        if capability not in IO_ALLOWED:
            raise _bad(
                where,
                f"io has {capability!r}, which is not allowed in v1",
                "use io: [media.read] or io: []",
            )
    return Effects("edit" if mutates else "read", tuple(touches), tuple(io))


def _parse_point_effects(value: Any, *, path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """effects_allowed: a list of levels, or {mutates, io}: [none, read] or [none, read, edit]."""
    if isinstance(value, list):
        bad = [v for v in value if v not in LEVELS]
        if bad:
            raise _fail(
                path,
                "effects_allowed",
                f"{bad[0]!r} is not an effects level",
                "use a list of none, read, edit",
            )
        return frozenset(value), frozenset()
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {"mutates", "io"})
        if unknown:
            raise _fail(
                path, "effects_allowed", f"unknown key {unknown[0]!r}", "use only mutates and io"
            )
        mutates = value.get("mutates")
        if not isinstance(mutates, bool):
            raise _fail(
                path,
                "effects_allowed",
                "mutates must be true or false",
                "write mutates: true or false",
            )
        io = _str_list(value.get("io"), where=str(path), field="io")
        bad_io = [i for i in io if i not in IO_ALLOWED]
        if bad_io:
            raise _fail(
                path,
                "effects_allowed",
                f"io has {bad_io[0]!r}, which is not allowed in v1",
                "use io: [media.read] or io: []",
            )
        levels = {"none", "read"} | ({"edit"} if mutates else set())
        return frozenset(levels), frozenset(io)
    raise _fail(
        path,
        "effects_allowed",
        "must be a list of levels or {mutates, io}",
        "write effects_allowed: {mutates: false, io: []}",
    )


# ---------------------------------------------------------------- points


def parse_point(path: Path, *, owner: str, text: str | None = None) -> ExtensionPoint:
    """Parse ``<pack>/extensions/points/<name>.yaml``; the point id is ``<owner>.<name>``."""
    path = Path(path)
    data = _load_yaml(path, text)
    _schema_check(
        path,
        data,
        _schema_file("point.json"),
        default_field="manifest",
        fix="use only the point fields in EXTENSIBILITY section 2.3 with the right types",
    )
    name = path.stem
    if not KEY_RE.match(name):
        raise _fail(
            path,
            "name",
            f"{name!r} is not a valid key",
            "rename the file to lowercase letters, digits, - or _",
        )
    if data["min_supported"] > data["version"]:
        raise _fail(
            path,
            "min_supported",
            f"{data['min_supported']} is above version {data['version']}",
            "set min_supported to at most version",
        )
    levels, io = _parse_point_effects(data["effects_allowed"], path=path)
    pack_root = _pack_root(path)
    base = path.parent

    def opt_ref(field: str) -> Ref | None:
        return (
            _ref(data[field], path=path, field=field, base_dir=base, pack_root=pack_root)
            if field in data
            else None
        )

    protocol = (
        None
        if data["protocol"] == "data"
        else _ref(data["protocol"], path=path, field="protocol", base_dir=base, pack_root=pack_root)
    )
    template = None
    if "template" in data:
        template = resolve_in_pack(
            data["template"],
            base_dir=base,
            pack_root=pack_root,
            what=f"template {data['template']!r}",
        )

    schema: dict[str, Any] = {}
    if isinstance(data.get("schema"), str):
        schema_path = resolve_in_pack(
            data["schema"], base_dir=base, pack_root=pack_root, what=f"schema {data['schema']!r}"
        )
        if not schema_path.is_file():
            raise _fail(
                path,
                "schema",
                f"file {data['schema']!r} does not exist beside the manifest",
                "create the JSON Schema file or fix the path",
            )
        try:
            loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _fail(
                path,
                "schema",
                f"{data['schema']!r} is not readable JSON ({exc})",
                "write a valid JSON Schema file",
            ) from None
        schema = _check_schema_object(loaded, path=path, field="schema")
    elif "schema" in data:
        schema = _check_schema_object(data["schema"], path=path, field="schema")

    return ExtensionPoint(
        id=f"{owner}.{name}",
        owner=owner,
        name=name,
        description=data["description"],
        version=data["version"],
        min_supported=data["min_supported"],
        side=data["side"],
        cardinality=data["cardinality"],
        effects_allowed=levels,
        io_allowed=io,
        schema=schema,
        protocol=protocol,
        harness=opt_ref("harness"),
        docs=opt_ref("docs"),
        template=template,
        path=path,
    )


# ---------------------------------------------------------------- contributions


def parse_contribution(
    path: Path,
    *,
    pack: str,
    pack_root: Path,
    scope: str,
    pack_version: str = "0.0.0",
    text: str | None = None,
    point: ExtensionPoint | None = None,
) -> Contribution:
    """Parse ``<pack>/extensions/<point>/<key>.yaml``. The point id is the folder name; the key is the stem."""
    path = Path(path)
    pack_root = Path(pack_root)
    key = path.stem
    if path.suffix not in (".yaml", ".yml"):
        raise _fail(path, "file", "contributions are YAML files", f"name it {key}.yaml")
    if not KEY_RE.match(key):
        raise _fail(
            path,
            "key",
            f"{key!r} is not a valid key",
            "rename the file to lowercase letters, digits, - or _, starting with a letter",
        )
    point_id = path.parent.name
    if point is not None and point.id != point_id:
        raise _fail(
            path,
            "point",
            f"the folder {point_id!r} is not the point {point.id!r}",
            "move the file into the folder of the point it contributes to",
        )
    if scope not in SCOPES:
        raise _fail(path, "scope", f"{scope!r} is not a scope", f"use one of: {', '.join(SCOPES)}")

    raw = _load_yaml(path, text)
    _schema_check(
        path,
        raw,
        _schema_file("contribution.json"),
        default_field="envelope",
        fix="fix the envelope field so it matches EXTENSIBILITY section 2.4",
    )
    if "name" in raw and raw["name"] != key:
        raise _fail(
            path,
            "name",
            f"{raw['name']!r} does not match the file name {key!r}",
            f"set name: {key}, or rename the file",
        )
    summary = raw["summary"]
    if "\n" in summary or "\r" in summary:
        raise _fail(path, "summary", "spans several lines", "write it on one line")
    if len(summary) > _SUMMARY_MAX:
        raise _fail(
            path,
            "summary",
            f"is {len(summary)} characters; the limit is {_SUMMARY_MAX}",
            "shorten it to one sentence",
        )

    api = raw["api"]
    effects = parse_effects(raw["effects"], where=f"{path}: effects")
    base = path.parent

    compatibility: SpecifierSet | None = None
    if "compatibility" in raw:
        spec = raw["compatibility"]
        if "" in [part.strip() for part in spec.split(",")]:
            raise _fail(
                path,
                "compatibility",
                f"{spec!r} has an empty part",
                'write a specifier set such as ">=1,<3"',
            )
        try:
            compatibility = SpecifierSet(spec)
        except InvalidSpecifier:
            raise _fail(
                path,
                "compatibility",
                f"{spec!r} is not a valid specifier set",
                'write a specifier set such as ">=1,<3"',
            ) from None

    entry: Ref | None = None
    if "entry" in raw:
        entry = _ref(raw["entry"], path=path, field="entry", base_dir=base, pack_root=pack_root)
    elif effects.level != "none":
        raise _fail(
            path,
            "entry",
            "is required unless effects is none",
            "add entry: file.py:func, or set effects: none for data",
        )

    deprecated = None
    if "deprecated" in raw:
        block = raw["deprecated"]
        deprecated = Deprecation(
            since=block["since"], use=block.get("use"), remove_after=block.get("remove_after")
        )

    fields = {k: v for k, v in raw.items() if k not in ENVELOPE_KEYS}

    if point is not None:
        if not point.min_supported <= api <= point.version:
            raise _fail(
                path,
                "api",
                f"targets {point.id} api {api}; this Astrid supports {point.min_supported}..{point.version}",
                f"set api to a supported version ({point.min_supported}..{point.version})",
            )
        if compatibility is not None and not compatibility.contains(str(api)):
            raise _fail(
                path,
                "compatibility",
                f"does not include api {api}",
                "widen compatibility to include api, or fix api",
            )
        if effects.level not in point.effects_allowed:
            raise _fail(
                path,
                "effects",
                f"level {effects.level!r} is not allowed by {point.id} (allowed: {', '.join(sorted(point.effects_allowed))})",
                "declare an allowed level, or use a different point",
            )
        bad_io = [i for i in effects.io if i not in point.io_allowed]
        if bad_io:
            raise _fail(
                path,
                "effects",
                f"io {bad_io[0]!r} is not allowed by {point.id}",
                "remove it from io",
            )
        if point.protocol is None and entry is not None:
            raise _fail(
                path,
                "entry",
                f"{point.id} is data-only and takes no entry",
                "remove entry, and give the data fields instead",
            )
        if not point.schema:
            if fields:
                first = next(iter(fields))
                raise _fail(path, first, f"{point.id} takes no fields", f"remove {first!r}")
        else:
            _schema_check(
                path,
                fields,
                point.schema,
                default_field="fields",
                fix=f"fix the field so it matches the schema of {point.id} (unknown fields are rejected)",
            )

    manifest_bytes = text.encode("utf-8") if text is not None else path.read_bytes()
    return Contribution(
        key=key,
        pack=pack,
        point=point_id,
        version=str(raw.get("version", pack_version)),
        digest=hashlib.sha256(manifest_bytes).hexdigest()[:12],
        scope=scope,  # type: ignore[arg-type]
        api=api,
        compatibility=compatibility,
        effects=effects,
        entry=entry,
        fields=fields,
        summary=summary,
        example=raw["example"],
        status=raw.get("status", "active"),  # type: ignore[arg-type]
        deprecated=deprecated,
        path=path,
    )
