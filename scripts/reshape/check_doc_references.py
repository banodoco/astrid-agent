#!/usr/bin/env python3
"""Docs <-> code integrity gate for agent-facing Astrid documentation.

Blocking checks (any error exits 1):

1. Capability references. An inline code span holding a qualified id
   ``<pack>.<capability>``, where ``<pack>`` is a first-party pack id, must
   resolve to a registered executor, orchestrator, renderer, planner, finalizer,
   or declared alias. The ids come from the SDK's own registry loaders, run
   offline. A first-party manifest that fails validation is reported as its own
   error; the rest of the registry still loads, so the other references are
   still checked. Dotted identifiers that are not capabilities (effect types,
   ``python -m pkg.module`` paths, CLI verbs or SDK method calls) must not be
   put in backticks.
2. CLI references. ``python3 -m astrid <family> <command> ...`` in fenced code
   and inline code spans must name a gateway family and a command the gateway
   parser accepts. An inline span that is itself a command (``timelines
   save``) is checked the same way. Retired verbs (``timelines shots group``)
   and unknown top-level options (``--brief``) fail. The
   tree comes from the same argparse builders the gateway dispatches through.
   Backup and doctor/status/setup subcommands are not introspectable without
   refactoring their inline parsers, so only their family name is checked.
3. Catalog freshness. ``scripts/gen_capability_index.py`` runs against a
   temporary copy of ``astrid/core/resources/skills/references/capabilities.md``.
   The gate fails if the regenerated file differs from the committed one.

Warning only (never fails the gate):

4. Untracked pack code. A file under ``astrid/packs/`` that git does not track
   but a tracked in-scope doc refers to. This is the a997fb37 failure mode.

Ignore marker. A line that is exactly ``<!-- doc-ref: historical -->`` skips
the line that follows it. When that line opens a fenced block, the whole block
is skipped. A marker at the end of a line skips that line. Use it only for
intentional historical mentions such as changelogs or retired-route notes.

Run with ``python3 scripts/reshape/check_doc_references.py`` (``make doc-refs``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CATALOG_PATH = "astrid/core/resources/skills/references/capabilities.md"
CATALOG_GENERATOR = "scripts/gen_capability_index.py"
PACKS_DIR = "astrid/packs"
MARKER = "<!-- doc-ref: historical -->"

# Docs an agent or pack author is told to follow. Globs are repo-relative; ``**``
# spans directories. Design notes, plans, and dated QA records under docs/ are
# deliberately out of scope; they are history, not instructions.
DOC_GLOBS: tuple[str, ...] = (
    "**/SKILL.md",
    "**/STAGE.md",
    "astrid/packs/**/skill/**/*.md",
    "docs/setup/*.md",
    "docs/getting-started.md",
    "docs/guides/**/*.md",
    "docs/reference/**/*.md",
    "docs/packs/**/*.md",
    "examples/**/*.md",
    "README.md",
    "AGENTS.md",
)

# Path fragments that never hold authored docs.
_EXCLUDED_PARTS = frozenset({".venv", "node_modules", "__pycache__", "out", "build", ".git"})

# A dotted token whose suffix is one of these is a file name, not a capability.
FILE_EXTENSIONS = frozenset(
    {
        "md", "json", "jsonl", "ndjson", "yaml", "yml", "toml", "txt", "py", "ts", "tsx",
        "js", "mjs", "cjs", "html", "css", "svg", "png", "jpg", "jpeg", "gif", "webp",
        "mp4", "mov", "webm", "mkv", "mp3", "wav", "m4a", "srt", "vtt", "csv", "tsv",
        "log", "lock", "sh", "pdf", "ini", "cfg", "env", "zip", "tar", "gz", "sql",
        "db", "pt", "safetensors", "bin", "tmp", "j2", "rs", "go",
    }
)

GATEWAY_OPTIONS = frozenset({"-h", "--help", "--version"})

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_SPAN_RE = re.compile(r"`([^`\n]+)`")
# Same grammar as astrid/core/pack/canonical.py::_QUALIFIED (underscores only).
_CAP_RE = re.compile(r"(?<![\w./-])(?<!-m )([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)(?![\w/-]|\.\w)")
_CLI_RE = re.compile(
    r"(?:^|(?<=[\s`(\"'=]))"
    r"(?:[\w./${}-]*python3?|\$\{?[A-Za-z_]*PY[A-Za-z_]*\}?)"
    r"\s+-m\s+astrid(?=\s|$)"
)
_COMMAND_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]*")
_SHORTHAND_RE = re.compile(r"\$?\s*([a-z][a-z0-9_-]*)\s+([a-z][a-z0-9-]*)(\s+\S+)*\s*")


@dataclass(frozen=True)
class Finding:
    """One gate result, rendered as ``LEVEL: path:line: message``."""

    level: str  # "error" or "warning"
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.level.upper()}: {self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Segment:
    """A piece of doc text the checks read: a fenced-code line or an inline span."""

    line: int
    kind: str  # "fence" or "span"
    text: str


@dataclass(frozen=True)
class CapabilitySurface:
    """What the SDK registries resolve, plus the first-party packs that failed to load."""

    pack_ids: frozenset[str]
    capability_ids: frozenset[str]
    pack_errors: Mapping[str, str]  # pack id -> load error (first-party manifests)
    registry_error: str | None = None


@dataclass
class CliTree:
    """Gateway command tree. ``None`` marks a node whose subcommands are not introspected."""

    top_level: dict[str, dict[str, Any] | None] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Surface loading (uses the same registry and parser code the gateway uses)
# ---------------------------------------------------------------------------


def _pack_ids_on_disk(packs_dir: Path) -> dict[str, str]:
    """Map each first-party pack id to its directory name, read from manifests.

    A manifest that cannot be parsed is skipped here; the registry load reports it.
    """
    from astrid.core.pack.manifest import load_manifest_mapping

    ids: dict[str, str] = {}
    if not packs_dir.is_dir():
        return ids
    for child in sorted(packs_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
            continue
        for name in ("pack.yaml", "pack.yml", "pack.json"):
            manifest = child / name
            if not manifest.is_file():
                continue
            try:
                data = load_manifest_mapping(manifest, manifest_kind="pack")
            except Exception:  # noqa: BLE001 - reported by the registry load, not here
                break
            if isinstance(data.get("id"), str):
                ids[data["id"]] = child.name
            break
    return ids


def _tolerant_discover(
    loaded: list[Any], failed: dict[str, str]
) -> Any:
    """Mirror ``astrid.core.pack.loader.discover_packs`` but keep going past bad packs.

    The strict loader raises on the first invalid first-party manifest, which
    would hide every other capability. Failures are recorded and reported by
    the gate instead.
    """
    from astrid.core.pack._common import PackValidationError
    from astrid.core.pack.loader import load_pack_manifest, pack_manifest_path, packs_root

    def discover(root: Any = None, *, include_hidden: bool = False) -> tuple[Any, ...]:
        source_root = Path(root) if root is not None else packs_root()
        if not source_root.is_dir():
            return ()
        packs: list[Any] = []
        seen: set[str] = set()
        for child in sorted(source_root.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            manifest_path = pack_manifest_path(child)
            if manifest_path is None:
                continue
            try:
                pack = load_pack_manifest(manifest_path)
            except PackValidationError as exc:
                failed[child.name] = str(exc)
                continue
            if pack.visibility == "hidden" and not include_hidden:
                continue
            if pack.id in seen:
                failed[child.name] = f"duplicate pack id {pack.id!r}"
                continue
            seen.add(pack.id)
            packs.append(pack)
        loaded.extend(packs)
        return tuple(packs)

    return discover


def load_capability_surface(packs_dir: Path = REPO_ROOT / PACKS_DIR) -> CapabilitySurface:
    """Resolve executor, orchestrator, and alias ids through the SDK registries."""
    from astrid.core.execution.executor import registry as executor_registry
    from astrid.core.execution.orchestrator import registry as orchestrator_registry
    from astrid.core.rendering import registry as rendering_registry

    disk_ids = _pack_ids_on_disk(packs_dir)
    loaded: list[Any] = []
    failed: dict[str, str] = {}
    discover = _tolerant_discover(loaded, failed)
    registry_error: str | None = None
    capability_ids: set[str] = set()
    try:
        with (
            mock.patch.object(executor_registry, "discover_packs", discover),
            mock.patch.object(orchestrator_registry, "discover_packs", discover),
            mock.patch.object(rendering_registry, "discover_packs", discover),
        ):
            executors = executor_registry.load_default_registry()
            orchestrators = orchestrator_registry.load_default_registry(executor_registry=executors)
            rendering = rendering_registry.load_default_registries()
        capability_ids.update(entry.id for entry in executors.list())
        capability_ids.update(entry.id for entry in orchestrators.list())
        for registry in rendering:
            capability_ids.update(entry.id for entry in registry.list())
        for pack in loaded:
            for alias in pack.aliases:
                if alias.get("kind") in {"executor", "orchestrator"} and alias.get("canonical_id") in capability_ids:
                    capability_ids.add(str(alias["alias"]))
    except Exception as exc:  # noqa: BLE001 - reported as a gate error, never swallowed
        registry_error = f"{type(exc).__name__}: {exc}"

    dir_to_id = {dir_name: pack_id for pack_id, dir_name in disk_ids.items()}
    pack_errors = {dir_to_id.get(dir_name, dir_name): message for dir_name, message in failed.items()}
    return CapabilitySurface(
        pack_ids=frozenset(disk_ids) | frozenset(pack.id for pack in loaded),
        capability_ids=frozenset(capability_ids),
        pack_errors=pack_errors,
        registry_error=registry_error,
    )


def _argparse_tree(parser: Any) -> dict[str, dict[str, Any]]:
    import argparse

    tree: dict[str, dict[str, Any]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                tree[name] = _argparse_tree(sub)
    return tree


def load_cli_tree(pack_ids: Iterable[str] = ()) -> CliTree:
    """Introspect the gateway parser tree offline, without a runtime connection.

    Installed pack ids are accepted as top-level routes by the gateway, but a
    pack route's commands are resolved by its own handler, so they are opaque.
    """
    import importlib

    from astrid.core.auth import _parser as auth_parser
    from astrid.core.cli.domain_product import FAMILY_PARSER_MODULES, PRODUCT_FAMILIES
    from astrid.core.gateway import SPRINT1_UNBOUND_ALLOWLIST_CONTRACT
    from astrid.core.gateway.dispatch import _TOP_LEVEL_HANDLERS
    from astrid.core.gateway.hivemind import PACK_COMMANDS
    from astrid.core.gateway.hivemind import _parser as pack_parser

    tree = CliTree()
    references = importlib.import_module(str(FAMILY_PARSER_MODULES["references"]))
    for family in PRODUCT_FAMILIES:
        module = importlib.import_module(str(FAMILY_PARSER_MODULES[family]))
        kwargs: dict[str, Any] = {}
        if family == "media":
            kwargs["reference_commands"] = references.COMMANDS
        tree.top_level[family] = _argparse_tree(module.build_parser(None, **kwargs))

    tree.top_level["auth"] = _argparse_tree(auth_parser())
    for spec in PACK_COMMANDS:
        tree.top_level[spec.pack] = _argparse_tree(pack_parser(spec.pack))

    # Routes whose subcommands are parsed inside their handlers.
    for name in (
        *_TOP_LEVEL_HANDLERS,
        *(row[0] for row in SPRINT1_UNBOUND_ALLOWLIST_CONTRACT if row[0] not in GATEWAY_OPTIONS),
        *pack_ids,
    ):
        tree.top_level.setdefault(name, None)
    return tree


# ---------------------------------------------------------------------------
# Doc scanning
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def in_doc_scope(rel: str) -> bool:
    if any(part in _EXCLUDED_PARTS for part in rel.split("/")):
        return False
    return any(_glob_to_regex(pattern).match(rel) for pattern in DOC_GLOBS)


def _git(repo_root: Path, *args: str) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def discover_docs(repo_root: Path = REPO_ROOT) -> list[str]:
    """Repo-relative in-scope docs in the working tree (tracked and untracked, not ignored)."""
    listed = _git(repo_root, "ls-files", "-co", "--exclude-standard")
    if listed is None:
        listed = [
            path.relative_to(repo_root).as_posix()
            for path in repo_root.rglob("*")
            if path.is_file()
        ]
    # ``git ls-files -c`` includes tracked paths deleted by another source
    # lane. Do not try to read those stale preimages while auditing the V3
    # docs tree; the source-closure owner will account for the deletion.
    return sorted(rel for rel in listed if in_doc_scope(rel) and (repo_root / rel).is_file())


def load_example_pack_capability_admissions(
    repo_root: Path = REPO_ROOT, docs: Iterable[str] = ()
) -> dict[str, frozenset[str]]:
    """Resolve declared capabilities for docs inside source-admitted example packs.

    Example packs deliberately live outside ``astrid/packs`` and therefore are
    not part of the first-party runtime registry. Their own docs may still use
    the qualified ids declared by the matching example ``pack.yaml``. Admission
    is path-scoped to ``examples/packs/<directory>/`` and declaration-scoped to
    the manifest; it never makes an example capability globally resolvable.
    """
    from astrid.core.pack.manifest import load_manifest_mapping

    examples_root = repo_root / "examples" / "packs"
    if not examples_root.is_dir():
        return {}
    doc_paths = tuple(docs)
    admissions: dict[str, frozenset[str]] = {}
    for pack_root in sorted(path for path in examples_root.iterdir() if path.is_dir()):
        manifest_path = next(
            (pack_root / name for name in ("pack.yaml", "pack.yml", "pack.json") if (pack_root / name).is_file()),
            None,
        )
        if manifest_path is None:
            continue
        try:
            manifest = load_manifest_mapping(manifest_path, manifest_kind="pack")
        except Exception:  # noqa: BLE001 - invalid sample manifests remain checker findings elsewhere
            continue
        pack_id = manifest.get("id")
        if not isinstance(pack_id, str) or not pack_id:
            continue
        declared: set[str] = set()
        for section in ("actions", "executors", "orchestrators"):
            entries = manifest.get(section)
            if isinstance(entries, Mapping):
                declared.update(f"{pack_id}.{name}" for name in entries if isinstance(name, str))
        advertised = manifest.get("capabilities")
        if isinstance(advertised, (list, tuple)):
            for name in advertised:
                if not isinstance(name, str):
                    continue
                declared.add(name if "." in name else f"{pack_id}.{name}")
        if not declared:
            continue
        prefix = f"examples/packs/{pack_root.name}/"
        for doc_path in doc_paths:
            if doc_path.startswith(prefix):
                admissions[doc_path] = frozenset(declared)
    return admissions


def iter_segments(text: str) -> Iterator[Segment]:
    """Yield checkable segments, honouring fences and the ignore marker."""
    fence: str | None = None
    skip_block = False
    skip_next = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence) and stripped.rstrip(fence[0]) == "":
                fence = None
                skip_block = False
                continue
            if not skip_block:
                yield Segment(lineno, "fence", line)
            continue
        opener = _FENCE_RE.match(line)
        marker_here = MARKER in line
        if opener is not None:
            fence = opener.group(1)
            skip_block = skip_next or marker_here
            skip_next = False
            continue
        suppressed = skip_next or marker_here
        skip_next = stripped == MARKER
        if suppressed:
            continue
        for match in _SPAN_RE.finditer(line):
            yield Segment(lineno, "span", match.group(1))


def _cli_invocations(text: str) -> Iterator[list[str]]:
    for match in _CLI_RE.finditer(text):
        rest = text[match.end():]
        rest = re.split(r"\||;|&&|\s#", rest, maxsplit=1)[0]
        cleaned = (token.strip("`'\"),.") for token in rest.split())
        yield [token for token in cleaned if token]


def check_cli_invocation(tokens: list[str], tree: CliTree) -> str | None:
    """Return an error message if the gateway would reject this invocation."""
    if not tokens:
        return None
    first = tokens[0]
    if first.startswith("-"):
        return None if first in GATEWAY_OPTIONS else f"unknown gateway option {first!r}"
    if first not in tree.top_level:
        return f"unknown gateway command {first!r}"
    node = tree.top_level[first]
    path = ["astrid", first]
    for token in tokens[1:]:
        if not node:  # leaf (no subcommands) or opaque node: remaining tokens are arguments
            return None
        if token.startswith("-") or not _COMMAND_TOKEN_RE.fullmatch(token):
            return None  # option or placeholder such as <ref>, {a,b}, or PROJECT
        if token not in node:
            return f"unknown command {token!r} for '{' '.join(path)}'"
        node = node[token]
        path.append(token)
    return None


def check_docs(
    docs: Mapping[str, str],
    surface: CapabilitySurface,
    tree: CliTree,
    example_capability_admissions: Mapping[str, frozenset[str]] | None = None,
) -> tuple[list[Finding], int, int]:
    """Run capability and CLI checks. Returns findings and (capability, CLI) reference counts."""
    findings: list[Finding] = []
    cap_refs = 0
    cli_refs = 0
    if surface.registry_error is not None:
        findings.append(
            Finding("error", CATALOG_GENERATOR, 1, f"capability registry failed to load: {surface.registry_error}")
        )
    example_capability_admissions = example_capability_admissions or {}
    for path, text in sorted(docs.items()):
        for segment in iter_segments(text):
            for tokens in _cli_invocations(segment.text):
                cli_refs += 1
                problem = check_cli_invocation(tokens, tree)
                if problem is not None:
                    shown = " ".join(["python3", "-m", "astrid", *tokens[:3]])
                    findings.append(
                        Finding("error", path, segment.line, f"CLI reference `{shown}`: {problem}")
                    )
            if segment.kind == "span" and not _CLI_RE.search(segment.text):
                shorthand = _SHORTHAND_RE.fullmatch(segment.text)
                if shorthand and shorthand.group(1) in tree.top_level and tree.top_level[shorthand.group(1)]:
                    tokens = segment.text.replace("$", "").split()
                    cli_refs += 1
                    problem = check_cli_invocation(tokens, tree)
                    if problem is not None:
                        findings.append(Finding("error", path, segment.line, f"CLI reference `{segment.text}`: {problem}"))
            if segment.kind != "span" or surface.registry_error is not None:
                continue
            for match in _CAP_RE.finditer(segment.text):
                pack, capability = match.group(1), match.group(2)
                if pack not in surface.pack_ids or capability in FILE_EXTENSIONS:
                    continue
                cap_refs += 1
                qualified = f"{pack}.{capability}"
                if qualified in surface.capability_ids:
                    continue
                if qualified in example_capability_admissions.get(path, frozenset()):
                    continue
                hint = ""
                if pack in surface.pack_errors:
                    hint = f" (pack {pack!r} failed to load: {surface.pack_errors[pack]})"
                findings.append(
                    Finding(
                        "error",
                        path,
                        segment.line,
                        f"unresolved capability `{qualified}`: not a registered executor, orchestrator, or alias{hint}",
                    )
                )
    for pack, message in sorted(surface.pack_errors.items()):
        findings.append(Finding("error", f"{PACKS_DIR}/{pack}/pack.yaml", 1, f"first-party pack failed to load: {message}"))
    return findings, cap_refs, cli_refs


# ---------------------------------------------------------------------------
# Catalog freshness
# ---------------------------------------------------------------------------


def start_catalog_check(
    repo_root: Path = REPO_ROOT, *, python: str = sys.executable
) -> Callable[[], list[Finding]]:
    """Start regenerating the catalog in the background; the returned callable waits for it.

    The generator loads every registry, so it runs in a subprocess while the
    gate does its own registry and doc work. Nothing is written to the committed
    catalog.
    """
    committed = repo_root / CATALOG_PATH
    generator = repo_root / CATALOG_GENERATOR
    if not committed.is_file():
        return lambda: [Finding("error", CATALOG_PATH, 1, "capability catalog is missing")]
    workdir = Path(tempfile.mkdtemp(prefix="doc-refs-"))
    target = workdir / "capabilities.md"
    shutil.copyfile(committed, target)
    proc = subprocess.Popen(
        [python, str(generator), "--target", str(target)],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def finish() -> list[Finding]:
        try:
            _stdout, stderr = proc.communicate(timeout=300)
            if proc.returncode != 0:
                last = ((stderr or "").strip().splitlines() or ["no output"])[-1]
                return [
                    Finding(
                        "error",
                        CATALOG_PATH,
                        1,
                        f"catalog generator failed (exit {proc.returncode}): {last}; fix the pack it loads",
                    )
                ]
            if target.read_bytes() != committed.read_bytes():
                return [
                    Finding(
                        "error",
                        CATALOG_PATH,
                        1,
                        f"capability catalog is stale; run {CATALOG_GENERATOR} and commit the result",
                    )
                ]
            return []
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return finish


def check_catalog(repo_root: Path = REPO_ROOT, *, python: str = sys.executable) -> list[Finding]:
    return start_catalog_check(repo_root, python=python)()


# ---------------------------------------------------------------------------
# Untracked pack code (warning only)
# ---------------------------------------------------------------------------


def _owner_of(rel: str) -> tuple[str, str, str | None] | None:
    """Return (pack, group, capability) for an astrid/packs file.

    ``group`` is the unit a doc would need committed: an executor or
    orchestrator folder when the file sits under one, otherwise its pack
    subdirectory. ``capability`` is the folder name for executor/orchestrator
    groups, which is the id a doc references as ``<pack>.<capability>``.
    """
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != "astrid" or parts[1] != "packs":
        return None
    pack = parts[2]
    if len(parts) >= 5 and parts[3] in {"executors", "orchestrators"}:
        return pack, "/".join(parts[:5]), parts[4]
    return pack, "/".join(parts[:4]), None


def check_untracked_pack_code(repo_root: Path, docs: Mapping[str, str]) -> list[Finding]:
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "--", PACKS_DIR)
    tracked_pack_files = _git(repo_root, "ls-files", "--", PACKS_DIR)
    if untracked is None or tracked_pack_files is None:
        return [Finding("warning", PACKS_DIR, 1, "git unavailable; untracked pack-code check skipped")]
    tracked_packs = {parts[2] for parts in (p.split("/") for p in tracked_pack_files) if len(parts) > 2}
    tracked_docs = set(_git(repo_root, "ls-files", "--") or []) & set(docs)
    groups: dict[str, tuple[str, str | None]] = {}
    for rel in untracked:
        owner = _owner_of(rel)
        if owner is None:
            continue
        pack, group, capability = owner
        if pack not in tracked_packs:
            # A pack with no tracked files is reported once, as a whole.
            group, capability = f"{PACKS_DIR}/{pack}", None
        groups.setdefault(group, (pack, capability))
    if not groups:
        return []

    qualified_refs: dict[str, list[tuple[str, int]]] = {}
    path_mentions: list[tuple[str, int, str]] = []
    for path in sorted(tracked_docs):
        for segment in iter_segments(docs[path]):
            if segment.kind == "span":
                for match in _CAP_RE.finditer(segment.text):
                    qualified = f"{match.group(1)}.{match.group(2)}"
                    qualified_refs.setdefault(qualified, []).append((path, segment.line))
        for lineno, line in enumerate(docs[path].splitlines(), start=1):
            if PACKS_DIR + "/" in line:
                path_mentions.append((path, lineno, line))

    findings: list[Finding] = []
    for group, (pack, capability) in sorted(groups.items()):
        hits: list[tuple[str, int]] = []
        if pack not in tracked_packs:
            hits = [hit for qualified, refs in qualified_refs.items() if qualified.startswith(pack + ".") for hit in refs]
        elif capability is not None:
            hits = qualified_refs.get(f"{pack}.{capability}", [])
        if not hits:
            hits = [(path, lineno) for path, lineno, line in path_mentions if group in line]
        if hits:
            doc, lineno = hits[0]
            findings.append(
                Finding(
                    "warning",
                    doc,
                    lineno,
                    f"{group} is untracked by git but referenced by a tracked doc; commit the pack code with the doc",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_gate(repo_root: Path = REPO_ROOT) -> tuple[list[Finding], dict[str, int]]:
    started = time.monotonic()
    finish_catalog = start_catalog_check(repo_root)
    doc_paths = discover_docs(repo_root)
    docs = {rel: (repo_root / rel).read_text(encoding="utf-8", errors="replace") for rel in doc_paths}
    surface = load_capability_surface(repo_root / PACKS_DIR)
    tree = load_cli_tree(surface.pack_ids)
    example_capability_admissions = load_example_pack_capability_admissions(repo_root, docs)
    findings, cap_refs, cli_refs = check_docs(docs, surface, tree, example_capability_admissions)
    findings.extend(finish_catalog())
    findings.extend(check_untracked_pack_code(repo_root, docs))
    stats = {
        "docs": len(docs),
        "capability_refs": cap_refs,
        "cli_refs": cli_refs,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return findings, stats


def main() -> int:
    findings, stats = run_gate()
    for finding in sorted(findings, key=lambda f: (f.level != "error", f.path, f.line, f.message)):
        print(finding.render())
    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    print(
        f"doc-refs: {errors} error(s), {warnings} warning(s); checked {stats['docs']} docs, "
        f"{stats['capability_refs']} capability refs, {stats['cli_refs']} CLI refs "
        f"in {stats['elapsed_ms'] / 1000:.1f}s"
    )
    if errors:
        print("doc-refs: fix the docs above, or mark intentional history with "
              f"'{MARKER}' on the preceding line.", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
