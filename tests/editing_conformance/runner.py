"""Editing conformance runner.

One scenario (YAML, format v1: see README.md) is applied through three front-ends (the `timelines`
CLI, a cut sheet, and a Python snippet). Each path runs on its own fresh working copy of a fixture.
``evaluate`` compares what the paths did: documents, undo steps, undo/redo, and the reports they print.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import difflib
import io
import json
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import render_sheet

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "timeline_editing"  # the shared fixtures
SCENARIO_FIXTURES = REPO / "tests" / "editing_scenarios" / "fixtures"  # scenario-local fixtures (first choice)
SCENARIOS = REPO / "tests" / "editing_scenarios"

PROJECT = "P"
TIMELINE = "t"
RESOLVED = ("p", "t", "rev-0")  # what the patched resolve_ids returns (the fixture's own ids)

PATHS = ("cli", "sheet", "python", "extension")
EXPECTS = ("change", "no-change", "refused")
CHECKS = ("journal", "runs", "changes", "reports", "equal", "steps", "undo", "redo", "check", "exists", "idempotent")
AREAS = ("timing", "structure", "look", "audio", "code")
TOP_KEYS = {"id", "source", "intent", "fixture", "tool", "expect", "idempotent", "cli", "sheet", "python", "check",
            "xfail", "extension"}
NO_CHANGE_OK_CODES = (0, 3)  # v1 CLI: 3 means "no change" (it ran)
XFAIL_KEYS = {"path", "check", "reason"}
MISSING_KEYS = {"missing"}
ID_RE = re.compile(r"(%s)\.[a-z0-9_-]+\.[a-z0-9_-]+" % "|".join(AREAS))


class ScenarioError(ValueError):
    """The scenario itself is malformed (not a conformance failure)."""


@dataclass(frozen=True)
class PathSpec:
    name: str
    missing: str | None = None  # why this front-end cannot do it today (audit ref); None when present
    commands: tuple[str, ...] = ()  # cli: one `timelines` command per entry (without the timeline/project)
    cuts: tuple[str, ...] | None = None  # sheet: only these cuts; None = the whole sheet
    replace: tuple[tuple[str, str], ...] = ()  # sheet: (old, new), old must occur exactly once
    text: str | None = None
    operation: str | None = None
    pack_roots: tuple[str, ...] = ()
    inputs: dict | None = None
    snippet: str | None = None  # python: runs with tl = Checkout.draft(P, t)


@dataclass(frozen=True)
class XFail:
    path: str
    check: str
    reason: str


@dataclass(frozen=True)
class Scenario:
    id: str
    source: str
    intent: str
    fixture: str
    expect: str
    paths: dict[str, PathSpec]
    check: tuple[str, ...]
    xfail: tuple[XFail, ...]
    tool: str | None = None  # the registered tool this scenario tests (stored only)
    idempotent: bool = False  # each path runs a second time on the same working copy; that run must be a no-change
    file: Path | None = None

    def present(self) -> list[str]:
        """Every path the scenario names, runnable or missing, in PATHS order."""
        return [name for name in PATHS if name in self.paths]

    def runnable(self) -> list[str]:
        return [name for name in self.present() if self.paths[name].missing is None]


@dataclass
class PathResult:
    name: str
    exit_codes: list[int]  # cli / sheet: one per command; python: empty
    stdout: str
    stderr: str
    error: str | None  # python exception, or an exception escaping a CLI handler
    base: str
    after: str
    undone: str
    redone: str
    new_steps: int
    undo_error: str | None
    redo_error: str | None
    checks: dict[str, tuple[bool, str]]  # expression -> (truthy, detail)

    records: list[dict] = dataclasses.field(default_factory=list)
    refusal_type: str | None = None
    second_changed: bool | None = None  # idempotent scenarios: did the second run change the document (None: not run)
    second_steps: int = 0  # undo steps the second run made (taken back before the undo/redo checks)
    second_stdout: str = ""
    second_exits: list[int] = dataclasses.field(default_factory=list)
    second_error: str | None = None

    @property
    def refused(self) -> bool:
        return self.refusal_type is not None or bool(self.error and self.error.startswith("refused:"))


# ---- loading -----------------------------------------------------------------------------


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML: {exc}") from exc
    scenario = parse_scenario(data, where=str(path))
    if scenario.id != path.stem:
        raise ScenarioError(f"{path}: id {scenario.id!r} must match the file name")
    return dataclasses.replace(scenario, file=path)


def parse_scenario(data: Any, where: str = "scenario") -> Scenario:
    """Validate a scenario mapping (as loaded from YAML) and return it. Raises ScenarioError."""
    if not isinstance(data, dict):
        raise ScenarioError(f"{where}: a scenario is a mapping")
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise ScenarioError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(TOP_KEYS)}")
    for key in ("id", "source", "intent", "fixture", "expect"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ScenarioError(f"{where}: {key!r} is required (a non-empty string)")
    sid = data["id"]
    if not ID_RE.fullmatch(sid):
        raise ScenarioError(f"{where}: id {sid!r} must be <area>.<op>.<case> with area in {AREAS}")
    if data["expect"] not in EXPECTS:
        raise ScenarioError(f"{where}: expect {data['expect']!r} must be one of {EXPECTS}")
    fixture_path(data["fixture"], where=where)
    tool = data.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool.strip()):
        raise ScenarioError(f"{where}: 'tool' is a non-empty string (the registered tool id) when present")
    idempotent = data.get("idempotent", False)
    if not isinstance(idempotent, bool):
        raise ScenarioError(f"{where}: idempotent is true or false")
    if idempotent and data["expect"] != "change":
        raise ScenarioError(f"{where}: idempotent applies only to expect: change, not {data['expect']!r}")

    paths = {name: _parse_path(name, data[name], where) for name in PATHS if name in data}
    if not any(spec.missing is None for spec in paths.values()):
        raise ScenarioError(f"{where}: at least one path must be present (not only missing)")

    check = data.get("check", [])
    if not isinstance(check, list) or not all(isinstance(e, str) and e.strip() for e in check):
        raise ScenarioError(f"{where}: check is a list of Python expressions")
    for expr in check:
        try:
            compile(expr, "<check>", "eval")
        except SyntaxError as exc:
            raise ScenarioError(f"{where}: check {expr!r} is not an expression: {exc}") from exc

    xfails = []
    seen: set[tuple[str, str]] = set()
    for entry in data.get("xfail", []) or []:
        if not isinstance(entry, dict) or set(entry) != XFAIL_KEYS:
            raise ScenarioError(f"{where}: each xfail entry is {{path, check, reason}}; got {entry!r}")
        path, check_name, reason = entry["path"], entry["check"], entry["reason"]
        if path not in PATHS:
            raise ScenarioError(f"{where}: xfail path {path!r} must be one of {PATHS}")
        if check_name not in CHECKS:
            raise ScenarioError(f"{where}: xfail check {check_name!r} must be one of {CHECKS}")
        if path not in paths:
            raise ScenarioError(f"{where}: xfail names path {path!r}, which the scenario does not have")
        if paths[path].missing is not None and check_name != "exists":
            raise ScenarioError(f"{where}: a missing path only has check 'exists', not {check_name!r}")
        if check_name == "reports" and data["expect"] == "refused":
            raise ScenarioError(f"{where}: 'reports' does not apply to expect: refused")
        if check_name == "equal" and data["expect"] != "change":
            raise ScenarioError(f"{where}: 'equal' only applies to expect: change")
        if not isinstance(reason, str) or not reason.strip():
            raise ScenarioError(f"{where}: xfail {path} {check_name} needs a reason (the audit reference)")
        if (path, check_name) in seen:
            raise ScenarioError(f"{where}: duplicate xfail {path} {check_name}")
        seen.add((path, check_name))
        xfails.append(XFail(path, check_name, reason))

    return Scenario(
        id=sid,
        source=data["source"],
        intent=data["intent"],
        fixture=data["fixture"],
        expect=data["expect"],
        paths=paths,
        check=tuple(check),
        xfail=tuple(xfails),
        tool=tool,
        idempotent=idempotent,
    )


def _parse_path(name: str, value: Any, where: str) -> PathSpec:
    if isinstance(value, dict) and "missing" in value:
        extra = set(value) - MISSING_KEYS
        if extra:
            raise ScenarioError(f"{where}: {name}: a missing path has only 'missing', not {sorted(extra)}")
        if not isinstance(value["missing"], str) or not value["missing"].strip():
            raise ScenarioError(f"{where}: {name}: 'missing' needs the reason (with the audit ref)")
        return PathSpec(name=name, missing=value["missing"])

    if name == "extension":
        if not isinstance(value, dict) or set(value) != {"operation", "pack_roots", "inputs"}:
            raise ScenarioError(f"{where}: extension requires operation, pack_roots and inputs")
        if not isinstance(value["operation"], str) or not value["operation"].startswith("timeline.operation/"):
            raise ScenarioError(f"{where}: extension requires a point-qualified operation identity")
        if not isinstance(value["inputs"], dict):
            raise ScenarioError(f"{where}: extension.inputs must be an object")
        roots = value["pack_roots"]
        if not isinstance(roots, list) or not roots or not all(isinstance(p, str) and p for p in roots):
            raise ScenarioError(f"{where}: extension.pack_roots must be explicit paths")
        return PathSpec(name=name, operation=value["operation"], inputs=value["inputs"], pack_roots=tuple(roots))

    if name == "cli":
        if isinstance(value, str) and value.strip():
            commands = (value,)
        elif isinstance(value, list) and value and all(isinstance(c, str) and c.strip() for c in value):
            commands = tuple(value)
        else:
            raise ScenarioError(f"{where}: cli is one command string or a non-empty list of them")
        for command in commands:
            if not shlex.split(command):
                raise ScenarioError(f"{where}: empty cli command")
        return PathSpec(name=name, commands=commands)

    if name == "python":
        if not isinstance(value, str) or not value.strip():
            raise ScenarioError(f"{where}: python is a non-empty snippet (or {{missing: ...}})")
        try:
            compile(value, "<python>", "exec")
        except SyntaxError as exc:
            raise ScenarioError(f"{where}: python snippet does not compile: {exc}") from exc
        return PathSpec(name=name, snippet=value)

    # sheet
    if not isinstance(value, dict):
        raise ScenarioError(f"{where}: sheet is a mapping (cuts, replace) or {{missing: ...}}")
    extra = set(value) - {"cuts", "replace", "text"}
    if extra:
        raise ScenarioError(f"{where}: sheet has unknown key(s) {sorted(extra)}; allowed: cuts, replace")
    text = value.get("text")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ScenarioError(f"{where}: sheet.text is a non-empty sheet")
    if text is not None and ("cuts" in value or "replace" in value):
        raise ScenarioError(f"{where}: sheet.text and cuts/replace are separate adapters")
    cuts = value.get("cuts")
    if cuts is not None and not (isinstance(cuts, list) and all(isinstance(c, str) for c in cuts)):
        raise ScenarioError(f"{where}: sheet.cuts is a list of cut ids")
    replace = []
    for pair in value.get("replace", []) or []:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(s, str) for s in pair) and pair[0]):
            raise ScenarioError(f"{where}: sheet.replace entries are [old, new] string pairs; got {pair!r}")
        replace.append((pair[0], pair[1]))
    return PathSpec(name=name, cuts=tuple(cuts) if cuts is not None else None, replace=tuple(replace), text=text)


# ---- running -----------------------------------------------------------------------------


def fixture_path(name: str, where: str = "fixture") -> Path:
    """tests/editing_scenarios/fixtures/NAME.json if it exists, else tests/fixtures/timeline_editing/NAME.json."""
    for folder in (SCENARIO_FIXTURES, FIXTURES):
        candidate = folder / f"{name}.json"
        if candidate.is_file():
            return candidate
    raise ScenarioError(f"{where}: no fixture {name!r}; looked in {SCENARIO_FIXTURES} and {FIXTURES}")


def canon(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False)


def _document() -> str:
    return canon(Checkout.draft(PROJECT, TIMELINE).document())


def _history_count() -> int:
    draft = tc.find_draft(PROJECT, TIMELINE)
    return len(sorted(tc._history_dir(draft).glob("*.json"))) if draft else 0


@contextlib.contextmanager
def _fresh(fixture: str):
    """A new temp data root and the resolve/fetch seams patched to a deep copy of the fixture."""
    bundle = json.loads(fixture_path(fixture).read_text(encoding="utf-8"))
    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as root:
        mp.setenv("BANODOCO_LOCAL_DATA_ROOT", root)
        mp.setattr(tc, "resolve_ids", lambda project, timeline, client=None: RESOLVED)
        mp.setattr(tc, "fetch_bundle",
                   lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle))
        yield Path(root)


def _run_cli(argv: list[str]) -> tuple[int, str, str, str | None]:
    """One `timelines` invocation: (exit code, stdout, stderr, exception text or None)."""
    out, err = io.StringIO(), io.StringIO()
    error = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            parsed = cli.build_parser(None).parse_args(argv)
            code = parsed.handler(parsed)
            code = 0 if code is None else int(code)
            refusal = getattr(parsed, "_edit_refusal", None)
            if refusal:
                error = f"refused:{refusal['type']}: {refusal['message']}"
        except SystemExit as exc:  # argparse errors and explicit exits
            code = 0 if exc.code is None else (exc.code if isinstance(exc.code, int) else 1)
        except Exception as exc:  # a crash is recorded separately and never counts as refusal
            code, error = 1, f"{type(exc).__name__}: {exc}"
    return code, out.getvalue(), err.getvalue(), error


def _checkout() -> None:
    code, _out, err, error = _run_cli(["checkout", TIMELINE, "--project", PROJECT])
    if code != 0 or error:
        raise RuntimeError(f"harness: checkout of the fresh copy failed: {error or err.strip()}")


def _eval_checks(exprs: tuple[str, ...]) -> dict[str, tuple[bool, str]]:
    tl = Checkout.draft(PROJECT, TIMELINE)  # re-loaded from disk, after the edit, before undo
    results: dict[str, tuple[bool, str]] = {}
    for expr in exprs:
        try:
            ok = bool(eval(compile(expr, "<check>", "eval"), {"tl": tl, "Checkout": Checkout}))
            results[expr] = (ok, "" if ok else "expression is false")
        except Exception as exc:
            results[expr] = (False, f"{type(exc).__name__}: {exc}")
    return results


def _undo_redo_cli(n: int) -> tuple[str | None, str | None, str, str]:
    """Undo n, then redo n, through the CLI. Returns (undo_error, redo_error, undone_doc, redone_doc)."""
    undo_error = _cli_error(["undo", TIMELINE, "--project", PROJECT, "--steps", str(n)])
    undone = _document()
    redo_error = _cli_error(["redo", TIMELINE, "--project", PROJECT, "--steps", str(n)])
    return undo_error, redo_error, undone, _document()


def _cli_error(argv: list[str]) -> str | None:
    code, _out, err, error = _run_cli(argv)
    if error:
        return error
    if code != 0:
        return f"exit {code}: {err.strip()}"
    return None


def _undo_redo_python(n: int) -> tuple[str | None, str | None, str, str]:
    undo_error = redo_error = None
    try:
        Checkout.draft(PROJECT, TIMELINE).undo(n)
    except Exception as exc:
        undo_error = f"{type(exc).__name__}: {exc}"
    undone = _document()
    try:
        Checkout.draft(PROJECT, TIMELINE).redo(n)
    except Exception as exc:
        redo_error = f"{type(exc).__name__}: {exc}"
    return undo_error, redo_error, undone, _document()


@dataclass
class _Rerun:
    """What a second run of a front-end did on the same working copy (idempotent scenarios)."""
    changed: bool
    steps: int
    stdout: str
    exits: list[int]
    error: str | None


def _run_commands(commands: tuple[str, ...]) -> tuple[list[int], str, str, str | None]:
    """The CLI commands in order: (exit codes, stdout, stderr, error). A refused command stops the list."""
    exits: list[int] = []
    stdout: list[str] = []
    stderr: list[str] = []
    error = None
    for command in commands:
        verb, *rest = shlex.split(command)
        # These scenarios compare edits; explicit sweep requests keep their integration behavior.
        if verb == "edit" and not {"--sweep", "--no-sweep"}.intersection(rest):
            rest.append("--no-sweep")
        code, out, err, exc = _run_cli([verb, TIMELINE, "--project", PROJECT, *rest])
        exits.append(code)
        stdout.append(out)
        stderr.append(err)
        if exc:
            error = exc
        if code != 0 or exc:
            break  # a refused command stops the list, as a shell would
    return exits, "".join(stdout), "".join(stderr), error


def _run_commands_again(commands: tuple[str, ...]) -> tuple[list[int], str, str | None]:
    exits, stdout, _stderr, error = _run_commands(commands)
    return exits, stdout, error


def _exec_snippet(snippet: str, tl: Checkout) -> tuple[str, str | None]:
    """The Python snippet with tl, then one save: (stdout, error). A snippet that raises saves nothing."""
    buf = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(buf):
            exec(snippet, {"tl": tl, "Checkout": Checkout})  # noqa: S102 - the scenario author's code
            tl.save()  # once: a script that raised before this point saves nothing
    except Exception as exc:
        from astrid.sdk.timeline._common import TimelineEditError
        prefix = "refused:" if isinstance(exc, TimelineEditError) else ""
        error = f"{prefix}{type(exc).__name__}: {exc}"
    return buf.getvalue(), error


def _rerun(run_again) -> _Rerun:
    """Run the same front-end a second time on the same working copy. Its undo steps are taken back, so the
    first run's undo/redo checks still mean what they meant. What the second run did is returned."""
    before = _document()
    steps_before = _history_count()
    exits, stdout, error = run_again()
    changed = _document() != before
    steps = _history_count() - steps_before
    if steps:
        undo_error = _cli_error(["undo", TIMELINE, "--project", PROJECT, "--steps", str(steps)])
        if undo_error:
            raise RuntimeError(f"harness: could not take back the second run's {steps} step(s): {undo_error}")
    return _Rerun(changed=changed, steps=steps, stdout=stdout, exits=exits, error=error)


def _run_cli_path(scenario: Scenario, spec: PathSpec) -> PathResult:
    with _fresh(scenario.fixture):
        _checkout()
        base = _document()
        exits, stdout, stderr, error = _run_commands(spec.commands)
        second = None
        if scenario.idempotent and error is None and all(code == 0 for code in exits):
            second = _rerun(lambda: _run_commands_again(spec.commands))
        return _finish(scenario, spec.name, exits, stdout, stderr, error, base, _undo_redo_cli, second)


def _run_sheet_path(scenario: Scenario, spec: PathSpec) -> PathResult:
    with _fresh(scenario.fixture) as root:
        _checkout()
        tl = Checkout.draft(PROJECT, TIMELINE)
        base = canon(tl.document())
        text = spec.text if spec.text is not None else render_sheet(tl, cuts=spec.cuts)
        for old, new in spec.replace:
            count = text.count(old)
            if count != 1:
                raise ScenarioError(
                    f"{scenario.id}: sheet.replace {old!r} occurs {count} time(s) in the exported sheet; "
                    "it must occur exactly once (a broken scenario)")
            text = text.replace(old, new, 1)
        sheet_file = root / "scenario.sheet"
        sheet_file.write_text(text, encoding="utf-8")
        code, out, err, exc = _apply_sheet(sheet_file)
        second = None
        if scenario.idempotent and exc is None and code == 0:
            second = _rerun(lambda: _apply_sheet_again(sheet_file))
        return _finish(scenario, spec.name, [code], out, err, exc, base, _undo_redo_cli, second)


def _apply_sheet(sheet_file: Path) -> tuple[int, str, str, str | None]:
    return _run_cli(["apply", TIMELINE, "--project", PROJECT, str(sheet_file)])


def _apply_sheet_again(sheet_file: Path) -> tuple[list[int], str, str | None]:
    code, out, _err, exc = _apply_sheet(sheet_file)
    return [code], out, exc


def _run_python_path(scenario: Scenario, spec: PathSpec) -> PathResult:
    with _fresh(scenario.fixture):
        _checkout()
        tl = Checkout.draft(PROJECT, TIMELINE)
        base = canon(tl.document())
        stdout, error = _exec_snippet(spec.snippet, tl)
        second = None
        if scenario.idempotent and error is None:
            second = _rerun(lambda: _rerun_python(spec))
        return _finish(scenario, spec.name, [], stdout, "", error, base, _undo_redo_python, second)


def _rerun_python(spec: PathSpec) -> tuple[list[int], str, str | None]:
    stdout, error = _exec_snippet(spec.snippet, Checkout.draft(PROJECT, TIMELINE))  # re-loaded from disk
    return [], stdout, error


def _invoke_extension(spec):
    from astrid.sdk.timeline.tools import resolve_operation, invoke_operation
    from astrid.sdk.timeline._common import TimelineEditError
    out, err = io.StringIO(), io.StringIO()
    error = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            operation = resolve_operation(
                spec.operation, project_root=REPO / "out/extension-harness",
                extra_pack_roots=tuple(str((REPO / p).resolve()) for p in spec.pack_roots),
            )
            preview = invoke_operation(Checkout.draft(PROJECT, TIMELINE), operation, spec.inputs)
            print(json.dumps(preview.to_json(), sort_keys=True))
            if not preview.changed:
                print("no change")
        except Exception as exc:
            prefix = "refused:" if isinstance(exc, TimelineEditError) else ""
            error = f"{prefix}{type(exc).__name__}: {exc}"
    return out.getvalue(), err.getvalue(), error


def _run_extension_path(scenario, spec):
    with _fresh(scenario.fixture):
        _checkout()
        base = _document()
        stdout, stderr, error = _invoke_extension(spec)
        second = None
        if scenario.idempotent and error is None:
            def again():
                out, _err, failure = _invoke_extension(spec)
                return [], out, failure
            second = _rerun(again)
        return _finish(scenario, spec.name, [], stdout, stderr, error, base, _undo_redo_python, second)


def _finish(scenario: Scenario, name: str, exits: list[int], stdout: str, stderr: str, error: str | None,
            base: str, undo_redo, second: _Rerun | None = None) -> PathResult:
    after = _document()
    from astrid.sdk.timeline import journal
    # Capture before undo/redo and check readers. Compare exact records, with
    # only the seven explicitly variable card fields removed.
    records = [row.to_json() for row in journal.read(journal.journal_path("p", "t"))]
    new_steps = _history_count()
    checks = _eval_checks(scenario.check)
    undo_error = redo_error = None
    undone = redone = after
    if new_steps >= 1:
        undo_error, redo_error, undone, redone = undo_redo(new_steps)
    extra = {}
    if second is not None:
        extra = dict(second_changed=second.changed, second_steps=second.steps, second_stdout=second.stdout,
                     second_exits=second.exits, second_error=second.error)
    return PathResult(
        name=name, exit_codes=exits, stdout=stdout, stderr=stderr, error=error,
        base=base, after=after, undone=undone, redone=redone, new_steps=new_steps,
        undo_error=undo_error, redo_error=redo_error, checks=checks, records=records, **extra,
    )


def run_case(scenario: Scenario, via: str) -> PathResult:
    """Run one front-end (via: "cli" | "sheet" | "python") on a fresh copy of the fixture.

    Raises ScenarioError for a broken scenario (a missing path, a replace that does not match once)."""
    if via not in PATHS:
        raise ScenarioError(f"via must be one of {PATHS}, not {via!r}")
    spec = scenario.paths.get(via)
    if spec is None:
        raise ScenarioError(f"{scenario.id}: no {via} path")
    if spec.missing is not None:
        raise ScenarioError(f"{scenario.id}: {via} is missing ({spec.missing}); it cannot run")
    if via == "cli":
        return _run_cli_path(scenario, spec)
    if via == "sheet":
        return _run_sheet_path(scenario, spec)
    if via == "extension":
        return _run_extension_path(scenario, spec)
    return _run_python_path(scenario, spec)


run_path = run_case  # alias, kept for existing callers


def run_scenario(scenario: Scenario) -> dict[str, PathResult]:
    return {name: run_case(scenario, name) for name in scenario.runnable()}


# ---- evaluating --------------------------------------------------------------------------


def _diff(a: str, b: str, a_name: str, b_name: str, lines: int = 30) -> str:
    diff = difflib.unified_diff(a.splitlines(), b.splitlines(), a_name, b_name, n=2, lineterm="")
    return "\n".join(list(diff)[:lines])


def evaluate(scenario: Scenario, results: dict[str, PathResult]) -> dict[tuple[str, str], str]:
    """The failures: (path, check) -> message. Empty when every check holds."""
    failures: dict[tuple[str, str], list[str]] = {}

    def fail(path: str, check: str, message: str) -> None:
        failures.setdefault((path, check), []).append(message)

    for name in scenario.present():
        if scenario.paths[name].missing is not None:
            fail(name, "exists", f"missing: {scenario.paths[name].missing}")

    ref_name = next((n for n in ("python", "cli", "sheet", "extension") if n in results), None)
    for name, r in results.items():
        _evaluate_path(scenario, r, fail, results[ref_name] if ref_name else None, ref_name)

    return {key: "\n".join(messages) for key, messages in failures.items()}


def _refused(r: PathResult, expect: str) -> bool:
    """A refusal: an exception, or an exit code outside what the expectation allows.

    For expect no-change, exit 3 (no change, v1 CLI) counts as having run."""
    if r.error is not None:
        return True
    allowed = NO_CHANGE_OK_CODES if expect == "no-change" else (0,)
    return any(code not in allowed for code in r.exit_codes)


def _evaluate_path(scenario: Scenario, r: PathResult, fail, ref: PathResult | None, ref_name: str | None) -> None:
    name, expect = r.name, scenario.expect
    changed = r.after != r.base

    if expect == "refused":
        if not r.refused:
            fail(name, "runs", f"did not refuse (expected a refusal); exit codes {r.exit_codes}")
        if changed:
            fail(name, "changes", "the document changed although the front-end refused\n"
                 + _diff(r.base, r.after, "base", name))
    else:
        if _refused(r, expect):
            detail = r.error or f"exit codes {r.exit_codes}: {(r.stderr or r.stdout).strip()[:300]}"
            fail(name, "runs", f"did not run cleanly: {detail}")
        if expect == "change" and not changed:
            fail(name, "changes", "the document did not change (a silent no-op)")
        if expect == "no-change" and changed:
            fail(name, "changes", "the document changed, but the edit should be a no-op\n"
                 + _diff(r.base, r.after, "base", name))
        if name != "python":  # Python has no report yet
            said = "no change" in r.stdout
            if said == changed:
                fail(name, "reports", "stdout says 'no change' but the document changed" if said
                     else "the document did not change, but stdout does not say 'no change'")

    if expect == "change":
        expected_steps = len(scenario.paths[name].commands) if name == "cli" else 1
    else:
        expected_steps = 0
    if r.new_steps != expected_steps:
        fail(name, "steps", f"{r.new_steps} new undo step(s), expected {expected_steps}")

    if expect == "change" and ref is not None and name != ref_name and r.after != ref.after:
        fail(name, "equal", f"differs from the reference ({ref_name}):\n" + _diff(ref.after, r.after, ref_name, name))

    if ref is not None and name != ref_name:
        left, right = comparable_records(ref.records), comparable_records(r.records)
        if left != right:
            fail(name, "journal", "journal differs from " + str(ref_name) + ":\n" + _diff(canon(left), canon(right), str(ref_name), name))
    if scenario.expect == "change" and not any(row.get("calls") for row in r.records):
        fail(name, "journal", "changed edit has no populated journal calls")

    if r.new_steps >= 1:
        if r.undo_error or r.undone != r.base:
            fail(name, "undo", f"undo {r.new_steps} did not restore the base"
                 + (f" (error: {r.undo_error})" if r.undo_error else "\n" + _diff(r.base, r.undone, "base", "undone")))
        if r.redo_error or r.redone != r.after:
            fail(name, "redo", f"redo {r.new_steps} did not restore the after-document"
                 + (f" (error: {r.redo_error})" if r.redo_error else "\n" + _diff(r.after, r.redone, "after", "redone")))

    if r.second_changed is not None:  # the scenario is idempotent and the first run was a clean change
        problems = []
        if r.second_error or any(code not in NO_CHANGE_OK_CODES for code in r.second_exits):
            problems.append(f"second run did not run cleanly: {r.second_error or r.second_exits}")
        if r.second_changed:
            problems.append("the second run changed the document")
        if r.second_steps:
            problems.append(f"the second run made {r.second_steps} undo step(s), expected 0")
        if name != "python" and "no change" not in r.second_stdout:
            problems.append("the second run did not say 'no change'")
        if problems:
            fail(name, "idempotent", "; ".join(problems))

    for expr, (ok, detail) in r.checks.items():
        if not ok:
            fail(name, "check", f"{expr}" + (f" ({detail})" if detail else ""))


JOURNAL_VARIABLE_FIELDS = frozenset({"source", "actor", "at", "id", "seq", "prev", "hash"})


def comparable_records(records):
    return [{key: value for key, value in record.items() if key not in JOURNAL_VARIABLE_FIELDS} for record in records]


def xfail_reasons(scenario: Scenario) -> dict[tuple[str, str], str]:
    return {(x.path, x.check): x.reason for x in scenario.xfail}


def classify(scenario: Scenario, failures: dict[tuple[str, str], str]) -> tuple[list, list, list]:
    """Split failures against the scenario's xfail list.

    Returns (unexpected, expected, fixed): unexpected failures are not listed (they fail the test);
    expected ones are listed as xfail; fixed ones are xfail entries that no longer fail (strict: remove them).
    A missing path's 'exists' failure is always expected.
    """
    listed = {(x.path, x.check) for x in scenario.xfail}
    implicit = {(name, "exists") for name in scenario.present() if scenario.paths[name].missing is not None}
    expected_keys = listed | implicit
    unexpected = sorted(k for k in failures if k not in expected_keys)
    expected = sorted(k for k in failures if k in expected_keys)
    fixed = sorted(k for k in listed if k not in failures)
    return unexpected, expected, fixed
