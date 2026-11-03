"""v10 docs<->CLI alignment.

Every ``astrid ...`` / ``python3 -m astrid ...`` command documented in the
agent-facing docs (AGENTS.md, _core/docs/SKILL.md, getting-started.md,
cli-journeys.md, every pack STAGE.md / SKILL.md, and every
docs/contracts/*.md / docs/packs/*.md guide) must be a real
command on the shipped eight-family gateway:

- the first token must be one of the eight families, ``help``, ``--help``,
  ``--version``, or a nested mount (``timelines shots``, ``media references``);
- product-family verbs must be accepted by that family's argparse parser
  (built through ``astrid.core.cli.domain_product.FAMILY_PARSER_MODULES`` with
  a ``None`` client — parsing never touches the database);
- ghost verbs from the retired task-mode CLI (``next``, ``status``, ``attach``,
  ``start``, ``ack``, ``executors``, ``orchestrators``, ``elements``,
  ``sessions``, ``packs``, ``skills``) must not appear as commands, and
  AGENTS.md + SKILL.md must not contain the retired invocation strings at all.

Pack STAGE.md / skill/SKILL.md files under
``astrid/packs/generation/actions/generate_image/`` and
``astrid/packs/generation/actions/generate_audio/`` are user-owned in-flight
files and are excluded from the extraction corpus.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import re
import shlex
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Outer launcher routes (``astrid.core.gateway._main_impl``) that are not
# dispatch families. ``help``/``--version`` print and exit; the rest are
# handled before dispatch.
_LAUNCHER_ROUTES = frozenset(
    {"help", "--help", "-h", "--version", "install", "status", "auth", "login", "logout", "revoke"}
)
# Launcher and operational routes whose surface is flags only: the route token
# is the whole command.
_FLAG_ONLY_ROUTES = frozenset({"install", "status", "login", "logout", "revoke", "doctor"})
# ``dev_operator.main`` builds its parser inline, so its verb set is mirrored
# here from that parser's subcommands.
_DEV_COMMANDS = frozenset({"status", "promote", "cache", "reclaim", "journey"})

# (family, nested) -> parser module for the manifest-declared nested mounts.
_NESTED_MOUNTS: dict[tuple[str, str], str] = {
    ("timelines", "shots"): "astrid.core.cli.domain_shots",
    ("media", "references"): "astrid.core.cli.domain_references",
}

_BACKUP_COMMANDS = frozenset(
    {"create", "restore", "export", "tombstone", "recover", "purge"}
)

# Retired task-mode invocation strings that must not appear verbatim.
_GHOST_STRINGS = (
    "astrid next",
    "astrid status",
    "astrid attach",
    "astrid start ",
    "astrid ack",
    "executors run",
    "orchestrators run",
)

# User-owned in-flight files: never edited by this sweep, so the test's
# extraction corpus excludes them (see module docstring).
_IN_FLIGHT_EXCLUDED = (
    _ROOT / "astrid/packs/generation/actions/generate_image",
    _ROOT / "astrid/packs/generation/actions/generate_audio",
)

_CORE_DOCS = (
    _ROOT / "AGENTS.md",
    _ROOT / "astrid/packs/_core/docs/SKILL.md",
    _ROOT / "docs/getting-started.md",
    _ROOT / "docs/guides/cli-journeys.md",
)


def _pack_docs() -> list[Path]:
    files: list[Path] = []
    for path in sorted((_ROOT / "astrid/packs").rglob("STAGE.md")):
        if not _is_excluded(path):
            files.append(path)
    for path in sorted((_ROOT / "astrid/packs").rglob("SKILL.md")):
        if not _is_excluded(path):
            files.append(path)
    return files


def _is_excluded(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in _IN_FLIGHT_EXCLUDED)


def _contract_docs() -> list[Path]:
    """Contract + pack-guide docs — historically a ghost-verb hiding spot."""
    files: list[Path] = []
    for rel in ("docs/contracts", "docs/packs"):
        files.extend(sorted((_ROOT / rel).rglob("*.md")))
    return files


def _all_docs() -> list[Path]:
    return list(_CORE_DOCS) + _pack_docs() + _contract_docs()


def _commands_from_line(raw_line: str) -> list[list[str]]:
    """Extract the ``astrid`` invocation from one shell-ish doc line.

    Only whitespace-delimited ``astrid`` tokens count as commands; path and
    module references (``astrid/packs/...``, ``astrid.sdk...``) are ignored.
    Trailing comments and shell metacharacters terminate the command. A
    literal ``...`` (signature shorthand) is not a command token.
    """
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        return []
    match = re.search(r"(?:^|\s)(?:python3\s+-m\s+)?astrid(?=\s|$)", line)
    if not match:
        return []
    tail = re.split(r"[;&|]", line[match.end():].strip())[0].strip()
    if not tail:
        return [["--help"]]
    try:
        tokens = shlex.split(tail, comments=False)
    except ValueError:
        # Unbalanced quotes: not a well-formed command example.
        return []
    # ``[...]`` is optional-argument notation in signatures, not shell syntax:
    # ``[--project PROJECT]`` documents the optional flag and its value, so the
    # brackets are stripped and the segment is validated as the real arguments.
    tokens = [token.strip("[]") for token in tokens]
    tokens = [token for token in tokens if token and token != "..."]
    if not tokens:
        return []
    return [tokens]


_FENCE_RE = re.compile(
    r"^```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_FENCE_LANGS = frozenset({"", "bash", "sh"})


def _extract_commands(text: str) -> list[list[str]]:
    """Every ``astrid`` command in fenced bash blocks and inline backticks.

    Fences are matched line-anchored so a closing fence delimiter cannot be
    re-paired as an opening fence, and fenced bodies are stripped before the
    inline-backtick pass — otherwise every bash block leaked back in as one
    giant inline "span" whose lines shlex'd into a single bogus command.
    """
    commands: list[list[str]] = []

    def _scan(block: str) -> None:
        # Join backslash continuations first: a command split across lines is
        # one documented invocation and must be validated as a whole, not
        # silently dropped by shlex's dangling-backslash error.
        pending = ""
        for line in block.splitlines():
            candidate = f"{pending} {line}".strip() if pending else line
            if candidate.endswith("\\"):
                pending = candidate[:-1].strip()
                continue
            pending = ""
            commands.extend(_commands_from_line(candidate))
        if pending:
            commands.extend(_commands_from_line(pending))

    for match in _FENCE_RE.finditer(text):
        if match.group(1) in _FENCE_LANGS:
            _scan(match.group(2))
    for span in re.findall(r"`([^`]*)`", _FENCE_RE.sub("", text)):
        if re.search(r"(?:^|\s)astrid(?=\s|$)", span):
            _scan(span)
    return commands


def _family_parser(module_name: str):
    module = importlib.import_module(module_name)
    builder = module.build_parser
    try:
        return builder(None)
    except TypeError:
        # Operational parsers (e.g. backup) take no client argument.
        return builder()


def _subcommands(parser) -> frozenset[str]:
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, __import__("argparse")._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _product_families() -> frozenset[str]:
    from astrid.core.cli.domain_product import PRODUCT_FAMILY_SET

    return PRODUCT_FAMILY_SET


def _product_parser_module(family: str) -> str:
    from astrid.core.cli.domain_product import FAMILY_PARSER_MODULES

    return FAMILY_PARSER_MODULES[family]


def _auth_parser():
    from astrid.core.auth import _parser

    return _parser()


def _pack_route_verbs() -> dict[str, frozenset[str]]:
    from astrid.core.gateway.hivemind import PACK_COMMANDS

    routes: dict[str, set[str]] = {}
    for spec in PACK_COMMANDS:
        routes.setdefault(spec.pack, set()).add(spec.command)
    return {pack: frozenset(verbs) for pack, verbs in routes.items()}


def _known_top_level() -> frozenset[str]:
    from astrid.core.gateway.dispatch import _top_level_commands

    return frozenset(_top_level_commands()) | _LAUNCHER_ROUTES | frozenset(_pack_route_verbs())


def _validate_verb(tokens: list[str], where: str, verbs: frozenset[str]) -> list[str]:
    family = tokens[0]
    if len(tokens) < 2:
        return [f"{where}: {family!r} without a verb: {tokens!r}"]
    if tokens[1] in {"--help", "-h"} or tokens[1] in verbs:
        return []
    return [f"{where}: {family!r} verb {tokens[1]!r} not in {sorted(verbs)}: {tokens!r}"]


def _validate_backup(tokens: list[str], where: str) -> list[str]:
    errors: list[str] = []
    if len(tokens) >= 2 and tokens[1] not in {"--help", "-h"}:
        backup_token = tokens[1]
        # The canonical operational help uses shell brace notation to
        # document the complete verb set in one executable-looking line.
        # Treat that notation as the documented alternatives rather than
        # requiring the braces to be a literal backup verb.
        if backup_token.startswith("{") and backup_token.endswith("}"):
            documented = frozenset(backup_token[1:-1].split(","))
            valid = documented == _BACKUP_COMMANDS
        else:
            valid = backup_token in _BACKUP_COMMANDS
        if not valid:
            errors.append(f"{where}: backup verb {tokens[1]!r} not in {sorted(_BACKUP_COMMANDS)}")
    return errors


def _validate_product(tokens: list[str], where: str) -> list[str]:
    errors: list[str] = []
    first = tokens[0]
    if len(tokens) < 2:
        # A bare family is documented surface only when its real parser runs
        # with no verb (``astrid preferences`` views both scopes); a family
        # that requires a verb stays a defect.
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                _family_parser(_product_parser_module(first)).parse_args([])
        except SystemExit:
            errors.append(f"{where}: {first!r} without a verb: {tokens!r}")
        return errors
    verb = tokens[1]
    if verb in {"--help", "-h"}:
        return errors
    mount = (first, verb)
    if mount in _NESTED_MOUNTS:
        # A bare mount reference (`timelines shots`, `media references`) is a
        # valid documented surface; a verb beneath it must be registered.
        if len(tokens) < 3:
            return errors
        nested_verb = tokens[2]
        if nested_verb in {"--help", "-h"}:
            return errors
        parser = _family_parser(_NESTED_MOUNTS[mount])
        if nested_verb not in _subcommands(parser):
            errors.append(
                f"{where}: nested verb {nested_verb!r} not in "
                f"{sorted(_subcommands(parser))}: {tokens!r}"
            )
        return errors
    parser = _family_parser(_product_parser_module(first))
    if verb not in _subcommands(parser):
        errors.append(
            f"{where}: verb {verb!r} not in {sorted(_subcommands(parser))}: {tokens!r}"
        )
        return errors
    # Belt and braces: the full documented command must parse. A nonzero
    # ``SystemExit`` whose argparse stderr reports a usage-surface defect —
    # unknown verb, unknown flag, missing argument — invalidates a documented
    # command. (``str(SystemExit)`` is just the exit code, never argparse's
    # message, so the captured stderr carries the detail.) Value-level data
    # errors on an otherwise real command — JSON decoding of a ``{...}``
    # shorthand placeholder, filesystem existence checks on example paths,
    # range/int conversion of placeholder values — are argument-level data
    # and out of scope here.
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            parser.parse_args(tokens[1:])
    except SystemExit as exc:  # argparse exits nonzero on any parse error
        detail = stderr.getvalue()
        usage_error = (
            "invalid choice" in detail
            or "unrecognized arguments" in detail
            or "arguments are required" in detail
        )
        if exc.code != 0 and usage_error:
            last = detail.strip().splitlines()
            errors.append(
                f"{where}: documented command does not parse: {tokens!r} "
                f"(exit {exc.code}: {last[-1] if last else 'no stderr'})"
            )
    return errors


def _validate_command(tokens: list[str], where: str) -> list[str]:
    first = tokens[0]
    if first not in _known_top_level():
        return [f"{where}: unknown top-level command {tokens!r}"]
    if first in {"--help", "-h", "--version", "help"} or first in _FLAG_ONLY_ROUTES:
        return []
    if first == "auth":
        return _validate_verb(tokens, where, _subcommands(_auth_parser()))
    if first == "dev":
        return _validate_verb(tokens, where, _DEV_COMMANDS)
    if first == "backup":
        return _validate_backup(tokens, where)
    pack_verbs = _pack_route_verbs()
    if first in pack_verbs:
        return _validate_verb(tokens, where, pack_verbs[first])
    return _validate_product(tokens, where)


def test_validator_surface_matches_gateway() -> None:
    """The validator's route table is the live gateway surface, not a snapshot."""
    assert _product_families() == frozenset(
        {"projects", "documents", "preferences", "timelines", "media", "tasks", "runs"}
    )
    assert _known_top_level() >= _product_families() | {"dev", "doctor", "backup"}
    assert "serve" not in _known_top_level()


def test_documented_commands_are_real_gateway_commands() -> None:
    """Every documented ``astrid`` command matches the eight-family surface."""
    errors: list[str] = []
    count = 0
    for path in _all_docs():
        text = path.read_text(encoding="utf-8")
        for tokens in _extract_commands(text):
            count += 1
            errors.extend(_validate_command(tokens, f"{path.relative_to(_ROOT)}"))
    assert count >= 40, f"extracted surprisingly few commands: {count}"
    assert not errors, "\n".join(errors)


def test_validator_rejects_invalid_verb_and_flag() -> None:
    """Regression (R2-B): invalid commands FAIL the validator.

    The old implementation inspected ``str(SystemExit)`` — which is just the
    exit code, never argparse's message — so a known verb with an unknown
    flag passed the gate silently.
    """
    # Unknown verb beneath a real family.
    assert _validate_command(["runs", "vacuum"], "regression")
    # Known verb with an unrecognized flag (the exact Sol finding).
    assert _validate_command(
        ["projects", "list", "--definitely-invalid"], "regression"
    )
    # Unknown verb beneath a nested mount.
    assert _validate_command(["media", "references", "frobnicate"], "regression")
    # The well-formed counterparts stay clean.
    assert _validate_command(["projects", "list", "--json"], "regression") == []
    # Value-level data errors on an otherwise real command stay out of scope:
    # `media import` rejects nonexistent paths at parse time, but that is
    # argument data, not a usage defect — the documented shape is real.
    assert (
        _validate_command(
            ["media", "import", "./no-such-input.png", "--project", "demo"],
            "regression",
        )
        == []
    )


def test_core_docs_have_no_retired_invocation_strings() -> None:
    """AGENTS.md and _core/docs/SKILL.md never mention the retired task-mode verbs."""
    for path in (_ROOT / "AGENTS.md", _ROOT / "astrid/packs/_core/docs/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        for ghost in _GHOST_STRINGS:
            assert ghost not in text, f"{path.name} contains {ghost!r}"


def test_pack_stages_do_not_reference_retired_verbs() -> None:
    """Pack STAGE.md / SKILL.md files contain no retired invocation strings."""
    offenders: list[str] = []
    for path in _pack_docs():
        text = path.read_text(encoding="utf-8")
        for ghost in _GHOST_STRINGS:
            if ghost in text:
                offenders.append(f"{path.relative_to(_ROOT)}: {ghost!r}")
    assert not offenders, "\n".join(offenders)
