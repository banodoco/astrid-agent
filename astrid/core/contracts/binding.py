"""Lossless, pure binding of declared capability inputs to process commands."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BOOLEAN_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOLEAN_FALSE = frozenset({"", "0", "false", "no", "off"})
_HOST_VALUE_NAMES = frozenset({"out", "run_root", "python_exec"})


class BindingError(ValueError):
    """A declared input could not be represented by the command contract."""


@dataclass(frozen=True)
class BindingResult:
    """Expanded command plus evidence of how declared inputs were handled."""

    argv: tuple[str, ...]
    cwd: str | None
    env: dict[str, str]
    bound_inputs: frozenset[str]
    auto_forwarded_inputs: frozenset[str]
    host_consumed_inputs: frozenset[str]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _metadata(metadata: Any) -> Mapping[str, Any]:
    return metadata if isinstance(metadata, Mapping) else {}


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _input_items(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=lambda item: (type(item).__name__, str(item))))
    return (value,)


def _stringify(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(item) for item in _input_items(value))
    return str(value)


def _stringify_declared_input(name: str, value: Any) -> str:
    if name == "shot_generation_recipe" and isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _stringify(value)


def _json_operand(name: str, value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BindingError(f"input {name!r} must be JSON-serializable") from exc


def _is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOLEAN_TRUE:
        return True
    if text in _BOOLEAN_FALSE:
        return False
    return bool(text)


def _input_flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _expand_text(value: str, replacements: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in replacements:
            raise BindingError(f"missing value for placeholder {{{name}}}")
        return replacements[name]

    return _PLACEHOLDER_RE.sub(replace, value)


def omit_absent_optional_flag_values(
    argv: Any, ports: Any, values: Mapping[str, Any],
) -> tuple[str, ...]:
    """Omit only literal flag/exact-placeholder pairs for absent optional ports.

    Required, unknown, standalone and embedded placeholders still reach the
    ordinary strict expander. Supplied values, including defaults, are intact.
    """
    absent: set[str] = set()
    for port in tuple(ports or ()):
        name = _port_name(port)
        if _field(port, "required", True) is False and not _has_value(values.get(name)):
            absent.add(name)
            alias = _field(port, "placeholder")
            if alias:
                absent.add(str(alias))
    tokens = tuple(str(part) for part in argv)
    result: list[str] = []
    index = 0
    while index < len(tokens):
        flag = tokens[index]
        placeholder = _PLACEHOLDER_RE.fullmatch(tokens[index + 1]) if index + 1 < len(tokens) else None
        if (re.fullmatch(r"--?[A-Za-z][A-Za-z0-9_-]*", flag)
                and placeholder is not None and placeholder.group(1) in absent):
            index += 2
            continue
        result.append(flag)
        index += 1
    return tuple(result)


def _port_name(port: Any) -> str:
    return str(_field(port, "name", ""))


def _port_type(port: Any) -> str:
    return str(_field(port, "type", "")).lower()


def _consumed_input_names(command: Any, ports: tuple[Any, ...]) -> set[str]:
    tokens: set[str] = set()
    for part in tuple(_field(command, "argv", ()) or ()):
        tokens.update(_PLACEHOLDER_RE.findall(str(part)))
    cwd = _field(command, "cwd")
    if cwd:
        tokens.update(_PLACEHOLDER_RE.findall(str(cwd)))
    for value in dict(_field(command, "env", {}) or {}).values():
        tokens.update(_PLACEHOLDER_RE.findall(str(value)))

    consumed = {
        str(_field(mapping, "input", ""))
        for mapping in tuple(_field(command, "input_args", ()) or ())
        if _field(mapping, "input", "")
    }
    for port in ports:
        name = _port_name(port)
        placeholder = _field(port, "placeholder")
        if name in tokens or (placeholder and str(placeholder) in tokens):
            consumed.add(name)
    return consumed


def _expand_one_input_arg(
    mapping: Any,
    *,
    values: Mapping[str, Any],
    port_by_name: Mapping[str, Any],
) -> list[str]:
    name = str(_field(mapping, "input", ""))
    value = values.get(name)
    if not _has_value(value):
        if bool(_field(mapping, "optional", False)):
            return []
        raise BindingError(f"missing mapped input {name!r}")
    port_type = _port_type(port_by_name.get(name))
    repeatable = bool(_field(mapping, "repeatable", False))
    # A structured JSON array is one value unless the mapping explicitly
    # declares repeated operands. In particular, [] must reach validation.
    items = (value,) if port_type == "json" and not repeatable else _input_items(value)
    if len(items) > 1 and not repeatable:
        raise BindingError(f"input {name!r} is not repeatable")

    flag = _field(mapping, "flag")
    is_boolean = _port_type(port_by_name.get(name)) in {"boolean", "bool"}
    expanded: list[str] = []
    for item in items:
        if not _has_value(item):
            continue
        if is_boolean and flag:
            if _is_truthy_flag(item):
                expanded.append(str(flag))
            continue
        if flag:
            expanded.append(str(flag))
        # JSON ports cross the command boundary as JSON, not Python's repr.
        # This matters for host-owned handoffs such as
        # ``materialized_objects``: repr(dict) uses single quotes and cannot
        # be parsed by the child executor, silently dropping verified source
        # previews.  Keep the normal string behavior for every other port.
        if port_type == "json":
            expanded.append(_json_operand(name, item))
        else:
            expanded.append(_stringify_declared_input(name, item))
    return expanded


def _validate_fixed_inputs(
    metadata: Mapping[str, Any],
    ports: tuple[Any, ...],
    values: Mapping[str, Any],
) -> None:
    """Enforce manifest-owned exact values for a narrow capability profile."""
    raw = metadata.get("fixed_inputs")
    if raw is None:
        return
    if not isinstance(raw, Mapping) or not raw:
        raise BindingError("metadata.fixed_inputs must be a non-empty object")
    port_by_name = {_port_name(port): port for port in ports}
    for name, expected in raw.items():
        if not isinstance(name, str) or not name:
            raise BindingError("metadata.fixed_inputs keys must be non-empty strings")
        port = port_by_name.get(name)
        if port is None:
            raise BindingError(f"metadata.fixed_inputs references undeclared input {name!r}")
        if _port_type(port) not in {"string", "text"}:
            raise BindingError(f"metadata.fixed_inputs input {name!r} must be a string port")
        if not isinstance(expected, str) or not expected:
            raise BindingError(f"metadata.fixed_inputs value for {name!r} must be a non-empty string")
        actual = values.get(name)
        if not isinstance(actual, str) or actual != expected:
            raise BindingError(
                f"fixed input {name!r} requires {expected!r}, got {actual!r}"
            )


def expand_command(
    command: Any,
    ports: Any,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
) -> BindingResult:
    """Expand placeholders and map every eligible declared input to transport."""

    declared_ports = tuple(ports or ())
    raw_argv = tuple(_field(command, "argv", ()) or ())
    raw_env = dict(_field(command, "env", {}) or {})
    raw_cwd = _field(command, "cwd")
    port_by_name = {_port_name(port): port for port in declared_ports}
    effective_values = dict(values)
    for port in declared_ports:
        name = _port_name(port)
        default = _field(port, "default")
        if name not in effective_values and default is not None:
            effective_values[name] = default

    _validate_fixed_inputs(_metadata(metadata), declared_ports, effective_values)

    replacements = {
        str(name): _stringify(value)
        for name, value in effective_values.items()
        if value is not None
    }
    for port in declared_ports:
        name = _port_name(port)
        placeholder = _field(port, "placeholder")
        if placeholder and name in effective_values and effective_values[name] is not None:
            replacements[str(placeholder)] = _stringify_declared_input(
                name, effective_values[name]
            )
        if name in effective_values and effective_values[name] is not None:
            replacements[name] = _stringify_declared_input(name, effective_values[name])

    argv = [_expand_text(part, replacements) for part in
            omit_absent_optional_flag_values(raw_argv, declared_ports, effective_values)]
    cwd = _expand_text(str(raw_cwd), replacements) if raw_cwd else None
    env = {str(key): _expand_text(str(value), replacements) for key, value in raw_env.items()}

    consumed = _consumed_input_names(command, declared_ports)
    appended: list[str] = []
    for mapping in tuple(_field(command, "input_args", ()) or ()):
        expanded = _expand_one_input_arg(
            mapping,
            values=effective_values,
            port_by_name=port_by_name,
        )
        if not expanded:
            continue
        before = _field(mapping, "before")
        if before is None:
            appended.extend(expanded)
            continue
        try:
            index = argv.index(str(before))
        except ValueError:
            appended.extend(expanded)
        else:
            argv[index:index] = expanded
    argv.extend(appended)

    config = _metadata(metadata)
    skipped = frozenset(str(name) for name in (config.get("auto_forward_skip") or ()))
    auto_forwarded: set[str] = set()
    if config.get("auto_forward_inputs") is not False:
        for port in declared_ports:
            name = _port_name(port)
            if name in _HOST_VALUE_NAMES or name in consumed or name in skipped:
                continue
            value = effective_values.get(name)
            if not _has_value(value):
                continue
            if _port_type(port) in {"boolean", "bool"}:
                if _is_truthy_flag(value):
                    argv.append(_input_flag(name))
                auto_forwarded.add(name)
                continue
            wrote_value = False
            is_json = _port_type(port) == "json"
            for item in ((value,) if is_json else _input_items(value)):
                if not _has_value(item):
                    continue
                operand = _json_operand(name, item) if is_json else _stringify_declared_input(name, item)
                argv.extend((_input_flag(name), operand))
                wrote_value = True
            if wrote_value:
                auto_forwarded.add(name)

    return BindingResult(
        argv=tuple(argv),
        cwd=cwd,
        env=env,
        bound_inputs=frozenset(consumed | auto_forwarded),
        auto_forwarded_inputs=frozenset(auto_forwarded),
        host_consumed_inputs=skipped,
    )


def assert_provided_inputs_bound(
    result: BindingResult,
    ports: Any,
    values: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
) -> None:
    """Refuse execution when a provided declared input has no disposition."""

    config = _metadata(metadata)
    skipped = {str(name) for name in (config.get("auto_forward_skip") or ())}
    unbound = [
        name
        for port in tuple(ports or ())
        if (name := _port_name(port)) not in _HOST_VALUE_NAMES
        and _has_value(
            values[name]
            if name in values
            else _field(port, "default")
        )
        and name not in result.bound_inputs
        and name not in skipped
    ]
    if unbound:
        raise BindingError(
            "provided declared input(s) are capability-irrelevant: "
            + ", ".join(unbound)
        )


__all__ = [
    "BindingError",
    "BindingResult",
    "assert_provided_inputs_bound",
    "expand_command",
]
