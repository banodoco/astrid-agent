#!/usr/bin/env python3
"""CPU candidate gate. NEVER makes paid calls or runs live generation.

All generation invocations use the public CLI with --dry-run. The other
commands only inspect Git, install CPU dependencies, or run declared CPU tests.
A FAIL blocks that cell; UNVERIFIED records missing coverage without blocking.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.catalog_dry_run import catalog_dry_run  # noqa: E402

CHECKS = ("catalog", "schema", "rebuild", "constructor")
# Recipe source names -> canonical VibeComfy pack slugs. Crosswalk documented
# in vibecomfy-agent-r13-inventory-2026-11-12.md; revisions always come from the recipe.
SOURCE_SLUGS = {
    "comfyui": "comfy_core",
    "h3-motion-context": "ComfyUI-H3-Motion-Context-MultiRef",
    "videohelpersuite": "ComfyUI-VideoHelperSuite",
    "comfyui-hunyuanvideo-foley": "ComfyUI-HunyuanVideo_Foley",
    "comfyui-qwentts": "ComfyUI-QwenTTS",
}
# Test ownership by engine source, not by cell name. Both files exercise the
# pinned QwenTTS source and its declared patches, without model weights.
CONSTRUCTORS = {"comfyui-qwentts": (
    "ASTRID_QWENTTS_CHECKOUT",
    ("tests/core/generation/test_source_patch_config_cpu.py",
     "tests/core/generation/test_source_patch_equivalence_cpu.py"),
)}
# Import packages needed by those tests. Versions belong to the engine lock.
ENGINE_PACKAGES = ("transformers", "einops", "librosa", "soundfile", "soxr",
                   "scipy", "sentencepiece", "pyyaml", "huggingface-hub", "attrs", "pydantic")
CPU_WHEELS = ("torch", "torchaudio")


@dataclass
class Cell:
    name: str
    entry: str
    mode: str
    template: str | None
    catalog_error: str = ""
    sources: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)


@dataclass
class Command:
    command: list[str]
    cwd: str
    exit_status: int
    log_path: str
    output: str = field(default="", repr=False)

    def record(self):
        return {key: value for key, value in asdict(self).items() if key != "output"}


class Runner:
    def __init__(self, logs: Path):
        self.logs = logs
        logs.mkdir(parents=True, exist_ok=True)
        self.commands: list[Command] = []

    def save(self, argv, cwd, rc, output):
        path = self.logs / f"{len(self.commands) + 1:03d}.log"
        path.write_text(output, encoding="utf-8")
        result = Command(list(map(str, argv)), str(cwd), rc, str(path), output)
        self.commands.append(result)
        return result

    def __call__(self, argv, *, cwd=ROOT, env=None, timeout=1200):
        argv = list(map(str, argv))
        # This is also enforced for the shared in-process CLI probe.
        if "generate" in argv and "--dry-run" not in argv:
            raise ValueError("preflight forbids generation without --dry-run")
        try:
            run = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, timeout=timeout, check=False)
            return self.save(argv, cwd, run.returncode, run.stdout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self.save(argv, cwd, -1, str(exc))


def outcome(status, reason="", commands=(), tests=()):
    return {"status": status, "reason": reason,
            "commands": [command.record() for command in commands], "tests": list(tests)}


def live_defaults():
    from astrid.core.generation.defaults import pack_candidates
    # Stage 1's tests/helpers/generation_defaults.py reads these same pack
    # declarations. Highest-priority non-video defaults are the live cell set.
    defaults = {}
    for candidate in pack_candidates():
        if candidate.kind != "video":
            key = (candidate.kind, candidate.mode)
            if key not in defaults or candidate.priority > defaults[key].priority:
                defaults[key] = candidate
    return defaults


def select_cells(registry, all_catalog=False, requested=()):
    defaults = live_defaults()
    by_template = {}
    for entry in registry.list_all(include_closed=True):
        for mode, spec in entry.modes.items():
            backend = spec.backends.get("local")
            if backend and backend.template:
                by_template.setdefault((backend.template, mode), []).append(entry.id)
    cells = []
    pairs = set()
    for (_, mode), candidate in sorted(defaults.items()):
        identifier = candidate.entry
        try:
            entry, spec = registry.get_by_mode(identifier, mode)
        except KeyError:
            matches = by_template.get((identifier, mode), [])
            if len(matches) != 1:
                cells.append(Cell(mode, identifier, mode, None,
                                  f"default {identifier}/{mode} has no unique catalog mapping"))
                continue
            entry, spec = registry.get_by_mode(matches[0], mode)
        backend = spec.backends.get("local")
        cells.append(Cell(mode, entry.id, mode, backend.template if backend else None))
        pairs.add((entry.id, mode))
    if all_catalog:
        for entry in registry.list_all(include_closed=True):
            for mode, spec in entry.modes.items():
                if (entry.id, mode) not in pairs:
                    backend = spec.backends.get("local")
                    cells.append(Cell(f"{entry.id}/{mode}", entry.id, mode,
                                      backend.template if backend else None))
    if requested:
        unknown = set(requested) - {cell.name for cell in cells}
        if unknown:
            raise ValueError("unknown cell(s): " + ", ".join(sorted(unknown)))
        cells = [cell for cell in cells if cell.name in requested]
    return cells


def recipe_facts():
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    sources = {source.name: source for source in recipe.sources}
    if sources["vibecomfy"].pin.revision != VIBECOMFY_ENGINE_REVISION:
        raise ValueError("Linux recipe and VibeComfy dependency authority disagree")
    identities = {slug: sources[name].pin.revision for name, slug in SOURCE_SLUGS.items()
                  if name in sources}
    if "comfy_core" not in identities:
        raise ValueError("recipe does not declare the required ComfyUI core identity")
    environment = next(env for env in recipe.environments if env.name == recipe.primary_environment)
    lock = ROOT / "astrid" / environment.requirements.packaged_lock.path
    return recipe, sources, identities, lock, VIBECOMFY_ENGINE_REVISION


def check_catalog(cells, registry, runner):
    pin = runner([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  "tests/test_vibecomfy_pin.py::test_pin_script_bump_to_the_current_revision_changes_no_bytes"])
    with tempfile.TemporaryDirectory(prefix="preflight-inputs-") as directory:
        for cell in cells:
            commands = [pin]
            reasons = []
            if pin.exit_status:
                reasons.append("current-revision pin no-op check failed: " + pin.log_path)
            if cell.catalog_error:
                reasons.append(cell.catalog_error)
            else:
                probe = catalog_dry_run(registry.get(cell.entry), cell.mode, Path(directory))
                commands.append(runner.save(["astrid", *probe.argv], ROOT, probe.exit_status,
                                            probe.stdout + probe.stderr))
                if probe.failure:
                    reasons.append(probe.failure)
            cell.checks["catalog"] = outcome("FAIL" if reasons else "PASS", "\n".join(reasons), commands)


def quarantine(path):
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    reason = " ".join(line.removeprefix("# reason:").strip() for line in lines if line.startswith("# reason:"))
    return {line.strip(): reason or f"listed in {path}" for line in lines
            if line.strip() and not line.startswith("#")}


def template_parameter(nodeid):
    match = re.search(r"\[([^\]]+)\]$", nodeid)
    if not match:
        return None
    # pytest adds declared-/sparse- to the template id for canonical builds.
    return re.sub(r"^(declared|sparse)-", "", match.group(1))


def test_kind(nodeid):
    return "rebuild" if "canonical_first_build" in nodeid else "schema"


def junit_results(path):
    rows = {}
    for case in ET.parse(path).iter("testcase"):
        filename = case.attrib.get("classname", "").replace(".", "/") + ".py"
        nodeid = filename + "::" + case.attrib["name"]
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        skipped = case.find("skipped")
        status = "FAIL" if problem is not None else "UNVERIFIED" if skipped is not None else "PASS"
        detail = problem if problem is not None else skipped
        reason = "" if detail is None else (detail.attrib.get("message", "") + "\n" + (detail.text or "")).strip()
        rows[nodeid] = {"nodeid": nodeid, "status": status, "reason": reason}
    return rows


def map_vibe_results(cells, selected, results, quarantined, command, identities):
    for cell in cells:
        for kind in ("schema", "rebuild"):
            ids = [nodeid for nodeid in selected
                   if template_parameter(nodeid) == cell.template and test_kind(nodeid) == kind]
            if not ids:
                cell.checks[kind] = outcome("UNVERIFIED", "no check of this kind declared for this cell's catalog template", [command])
                continue
            tests = []
            for nodeid in ids:
                row = dict(results.get(nodeid, {"nodeid": nodeid, "status": "FAIL", "reason": "selected test produced no result"}))
                # Recipe-selected identities are mandatory even if a legacy
                # workflow identity failure was quarantined before the re-pin.
                missing_recipe_identity = any(
                    f"no object_info captured for {slug} at {commit}" in row["reason"]
                    for slug, commit in identities.items())
                if nodeid in quarantined and not missing_recipe_identity:
                    row.update(status="UNVERIFIED", reason=quarantined[nodeid])
                tests.append(row)
            status = "FAIL" if any(t["status"] == "FAIL" for t in tests) else (
                "UNVERIFIED" if any(t["status"] == "UNVERIFIED" for t in tests) else "PASS")
            reason = "\n".join(f"{t['nodeid']}: {t['reason']}" for t in tests if t["reason"])
            cell.checks[kind] = outcome(status, reason, [command], tests)


def check_vibe(cells, runner, checkout, python, identities, revision):
    if checkout is None:
        for cell in cells:
            for kind in ("schema", "rebuild"):
                cell.checks[kind] = outcome("UNVERIFIED", "set --vibecomfy-checkout or ASTRID_VIBECOMFY_CHECKOUT")
        return
    checkout = checkout.resolve()
    head = runner(["git", "rev-parse", "HEAD"], cwd=checkout)
    clean = runner(["git", "status", "--porcelain", "--untracked-files=all"], cwd=checkout)
    if head.exit_status or head.output.strip() != revision or clean.exit_status or clean.output.strip():
        for cell in cells:
            for kind in ("schema", "rebuild"):
                cell.checks[kind] = outcome("FAIL", f"refusing VibeComfy checkout: requires clean HEAD {revision}; observed {head.output.strip()}", [head, clean])
        return False
    if python is None:
        candidates = [checkout / ".venv/bin/python", Path(str(checkout) + ".env/venv/bin/python")]
        python = next((path for path in candidates if path.is_file()), candidates[0])
    pins = runner.logs / "pack-identity-commits.json"
    pins.write_text(json.dumps(identities, indent=2) + "\n", encoding="utf-8")
    base = [str(python), "-m", "pytest", "-p", "no:cacheprovider", "-m", "preflight",
            "--pack-identity-commits=" + str(pins)]
    collection = runner([*base, "--collect-only", "-q", "tests/test_ready_templates.py"], cwd=checkout)
    if collection.exit_status not in (0, 5):  # pytest's 5 means no tests selected
        raise RuntimeError("VibeComfy preflight collection failed: " + collection.log_path)
    selected = [line.strip() for line in collection.output.splitlines()
                if line.startswith("tests/") and "::" in line
                and template_parameter(line.strip()) in {cell.template for cell in cells if cell.template}]
    if not selected:
        for cell in cells:
            for kind in ("schema", "rebuild"):
                cell.checks[kind] = outcome("UNVERIFIED", "no preflight checks declared for the catalog template", [collection])
        return True
    report = runner.logs / "vibecomfy.xml"
    command = runner([*base, "-q", "-rA", "--junitxml=" + str(report), *selected], cwd=checkout)
    if not report.is_file() or command.exit_status not in (0, 1):
        raise RuntimeError("VibeComfy preflight harness failed: " + command.log_path)
    results = junit_results(report)
    # Unexpected/collection errors must never be hidden by passing cases.
    if any(nodeid not in selected for nodeid in results):
        raise RuntimeError("VibeComfy reported an unexpected test/collection error: " + command.log_path)
    if command.exit_status and not any(row["status"] == "FAIL" for row in results.values()):
        raise RuntimeError("VibeComfy failed outside its test outcomes: " + command.log_path)
    map_vibe_results(cells, selected, results,
                     quarantine(checkout / "tests/quarantine/offline_identity_provenance.txt"), command, identities)
    # A catalog template with no declared VibeComfy case is UNVERIFIED for
    # that cell; it must not crash source inspection for unrelated cells.
    templates = sorted({template_parameter(nodeid) for nodeid in selected})
    source_code = (
        "import json; from vibecomfy.registry.ready import workflow_from_ready; "
        "from vibecomfy.custom_node_refs import workflow_lock_entries; "
        "print(json.dumps({t:[p.slug or p.name for p in workflow_lock_entries(workflow_from_ready(t))] "
        "for t in json.loads(__import__('sys').argv[1])}))")
    sources = runner([python, "-c", source_code, json.dumps(templates)], cwd=checkout)
    if sources.exit_status:
        raise RuntimeError("could not read template source ownership: " + sources.log_path)
    by_template = json.loads(sources.output)
    for cell in cells:
        cell.sources = [source for source, slug in SOURCE_SLUGS.items()
                        if slug in by_template.get(cell.template, [])]
    return True


def locked_versions(lock):
    text = lock.read_text(encoding="utf-8")
    versions = dict(re.findall(r"^([\w-]+)==([^\s\\]+)", text, re.M))
    for name in CPU_WHEELS:
        match = re.search(rf"^{name} @ \S+/{name}-([^%+\s]+)(?:%2B|\+)", text, re.M)
        if not match:
            raise ValueError(f"engine lock does not pin {name}")
        versions[name] = match.group(1)
    for name in (*ENGINE_PACKAGES, *CPU_WHEELS):
        if name not in versions:
            raise ValueError(f"engine lock does not pin {name}")
    return versions


def require_success(command):
    if command.exit_status:
        raise RuntimeError("command failed: " + command.log_path)
    return command


def engine_python(runner, venv, cache, lock, recipe):
    versions = locked_versions(lock)
    if venv is None:
        constraints = runner.logs / "engine-constraints.txt"
        constraints.write_text("\n".join(f"{name}=={version}" for name, version in sorted(versions.items())
                                          if name not in CPU_WHEELS) + "\n", encoding="utf-8")
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        venv = cache / ("engine-" + digest)
        python = venv / "bin/python"
        if not python.is_file():
            environment = next(e for e in recipe.environments if e.name == recipe.primary_environment)
            require_success(runner(["uv", "venv", "--python", environment.python_version, venv]))
        require_success(runner(["uv", "pip", "install", "--python", python,
                                "--index-url", "https://download.pytorch.org/whl/cpu",
                                "--no-deps",
                                *(f"{name}=={versions[name]}" for name in CPU_WHEELS)]))
        require_success(runner(["uv", "pip", "install", "--python", python,
                                "--constraint", constraints,
                                *(f"{name}=={versions[name]}" for name in ENGINE_PACKAGES),
                                *(f"{name}=={versions[name]}+cpu" for name in CPU_WHEELS),
                                "pytest", "jsonschema", "referencing", "sox", "onnxruntime", "pip"]))
        # Agent's conftest imports Runtime protocol support. Reuse its existing
        # installer, which reads runtime.lock.json; don't maintain another pin.
        require_success(runner([python, ROOT / "scripts/ci_install_runtime.py"]))
    python = venv / "bin/python"
    probe = require_success(runner([python, "-c",
        "import json,importlib.metadata as m; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))"]))
    installed = json.loads(probe.output)
    normalized = {key.lower().replace("_", "-"): value for key, value in installed.items()}
    required = {*ENGINE_PACKAGES, *CPU_WHEELS}
    errors = []
    for name, version in versions.items():
        if name not in normalized and name not in required:
            continue  # CPU proof installs the dependency closure it uses, not CUDA's full environment.
        actual = normalized.get(name, "missing")
        compared = actual.split("+", 1)[0] if name in CPU_WHEELS else actual
        if compared != version:
            errors.append(f"{name}: requires {version}, installed {actual}")
    if errors:
        raise ValueError("engine venv differs from lock: " + "; ".join(errors))
    for name in CPU_WHEELS:
        if normalized[name] != versions[name] + "+cpu":
            raise ValueError(f"{name} must be the CPU wheel at {versions[name]}")
    return python, installed


def fetch_source(source, cache, runner):
    checkout = cache / "sources" / source.name / source.pin.revision
    if not (checkout / ".git").exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        require_success(runner(["git", "init", checkout]))
    head = runner(["git", "-C", checkout, "rev-parse", "HEAD"])
    if head.exit_status:
        # An interrupted first fetch leaves an initialized, empty cache repo.
        require_success(runner(["git", "-C", checkout, "fetch", "--depth", "1", source.pin.repository, source.pin.revision]))
        require_success(runner(["git", "-C", checkout, "checkout", "--detach", "FETCH_HEAD"]))
    head = require_success(runner(["git", "-C", checkout, "rev-parse", "HEAD"]))
    clean = require_success(runner(["git", "-C", checkout, "status", "--porcelain", "--untracked-files=all"]))
    if head.output.strip() != source.pin.revision or clean.output.strip():
        raise ValueError("engine source is not clean at recipe pin: " + str(checkout))
    return checkout


def check_constructors(cells, runner, venv, cache, lock, recipe, sources):
    for cell in cells:
        reason = ("cannot map engine sources: " + cell.catalog_error if cell.catalog_error else
                  "no constructor check declared for this cell's engine sources")
        cell.checks["constructor"] = outcome("UNVERIFIED", reason)
    affected = [cell for cell in cells if set(cell.sources) & CONSTRUCTORS.keys()]
    if not affected:
        return {}
    start = len(runner.commands)
    try:
        python, installed = engine_python(runner, venv, cache, lock, recipe)
        for name, (variable, paths) in CONSTRUCTORS.items():
            owners = [cell for cell in affected if name in cell.sources]
            if not owners:
                continue
            checkout = fetch_source(sources[name], cache, runner)
            env = dict(os.environ, **{variable: str(checkout), "HF_HUB_OFFLINE": "1",
                                      "TRANSFORMERS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": ""})
            report = runner.logs / (name + ".xml")
            command = runner([python, "-m", "pytest", "-q", "-rA", "-m", "engine_deps",
                              "-p", "no:cacheprovider", "--junitxml=" + str(report), *paths], env=env)
            tests = list(junit_results(report).values()) if report.is_file() else []
            status = "FAIL" if command.exit_status or not tests or any(t["status"] != "PASS" for t in tests) else "PASS"
            reason = "\n".join(f"{t['nodeid']}: {t['reason']}" for t in tests if t["status"] != "PASS")
            if status == "FAIL" and not reason:
                reason = "engine_deps harness failed or ran no tests: " + command.log_path
            for cell in owners:
                cell.checks["constructor"] = outcome(status, reason, runner.commands[start:], tests)
        return installed
    except (ValueError, RuntimeError) as exc:
        for cell in affected:
            cell.checks["constructor"] = outcome("FAIL", str(exc), runner.commands[start:])
        return {}


def new_report():
    return {"schema": "astrid.cpu-preflight.v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "candidate": {}, "engine_versions": {}, "cells": [], "commands": [], "ok": False}


def run_gate(cells, registry, runner, args):
    report = new_report()
    try:
        report["candidate"]["agent_revision"] = require_success(runner(["git", "rev-parse", "HEAD"])).output.strip()
        report["candidate"]["agent_worktree"] = require_success(runner(
            ["git", "status", "--porcelain", "--untracked-files=all"])).output.strip()
        recipe, sources, identities, lock, revision = recipe_facts()
        report["candidate"].update(vibecomfy_revision=revision, pack_identity_commits=identities,
                                    engine_lock=str(lock), engine_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest())
        verified_checkout = check_vibe(cells, runner, args.vibecomfy_checkout, args.vibecomfy_python, identities, revision)
        if verified_checkout is False:
            for cell in cells:
                for kind in ("catalog", "constructor"):
                    cell.checks[kind] = outcome("UNVERIFIED", "VibeComfy checkout refused; dependent checks were not run")
            report["cells"] = [asdict(cell) for cell in cells]
            report["commands"] = [command.record() for command in runner.commands]
            report["ok"] = False
            return report
        if verified_checkout:
            checkout = args.vibecomfy_checkout.resolve()
            # Bind the public CLI to the source whose HEAD was just checked,
            # even when the caller's Agent environment has another PYTHONPATH.
            for name, module in list(sys.modules.items()):
                if name == "vibecomfy" or name.startswith("vibecomfy."):
                    origin = getattr(module, "__file__", None)
                    if origin and not Path(origin).resolve().is_relative_to(checkout):
                        raise RuntimeError("Agent process already imported VibeComfy outside the pinned checkout")
            sys.path.insert(0, str(checkout))
            os.environ["ASTRID_VIBECOMFY_CHECKOUT"] = str(checkout)
            report["candidate"]["vibecomfy_checkout"] = str(checkout)
        check_catalog(cells, registry, runner)
        report["engine_versions"] = check_constructors(cells, runner, args.engine_venv, args.cache_dir, lock, recipe, sources)
    except Exception as exc:  # noqa: BLE001 - a harness crash must fail every cell
        reason = f"preflight harness crashed: {type(exc).__name__}: {exc}"
        crash = runner.save([sys.executable, str(Path(__file__))], ROOT, -1, traceback.format_exc())
        for cell in cells:
            for kind in CHECKS:
                cell.checks[kind] = outcome("FAIL", reason, [crash])
    report["cells"] = [asdict(cell) for cell in cells]
    report["commands"] = [command.record() for command in runner.commands]
    report["ok"] = bool(cells) and not any(check["status"] == "FAIL" for cell in cells for check in cell.checks.values())
    return report


def print_table(report):
    rows = [["CELL", "ENTRY / MODE", *[key.upper() for key in CHECKS]]]
    for cell in report["cells"]:
        rows.append([cell["name"], cell["entry"] + " / " + cell["mode"],
                     *[cell["checks"][kind]["status"] for kind in CHECKS]])
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))
    for cell in report["cells"]:
        for name, check in cell["checks"].items():
            if check["reason"]:
                print(f"\n{cell['name']} / {name}: {check['status']} — {check['reason']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", action="append", default=[], help="restrict to a named cell (repeatable)")
    parser.add_argument("--all-catalog", action="store_true", help="add every catalog entry/mode, including closed entries")
    parser.add_argument("--json", type=Path, help="write the table, reasons, commands and log paths as JSON")
    parser.add_argument("--vibecomfy-checkout", type=Path, default=os.environ.get("ASTRID_VIBECOMFY_CHECKOUT"))
    parser.add_argument("--vibecomfy-python", type=Path, help="VibeComfy's own test interpreter; defaults to its .venv or sibling .env/venv")
    parser.add_argument("--engine-venv", type=Path, help="reuse a CPU engine environment matching the engine lock")
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "astrid/preflight")
    args = parser.parse_args(argv)
    # Commands run in both repositories; resolve user paths before changing
    # subprocess cwd so relative cache/report/interpreter paths stay usable.
    for name in ("cache_dir", "json", "vibecomfy_checkout", "vibecomfy_python", "engine_venv"):
        value = getattr(args, name)
        if value is not None:
            # Resolving bin/python's symlink discards its virtual environment.
            path = value.expanduser()
            setattr(args, name, path.absolute() if name == "vibecomfy_python" else path.resolve())
    if args.vibecomfy_checkout is not None:
        # The public CLI's dependency selection must inspect the same checkout
        # as the VibeComfy checks, including when selected through the flag.
        os.environ["ASTRID_VIBECOMFY_CHECKOUT"] = str(args.vibecomfy_checkout.resolve())
    from astrid.core.model_catalog.registry import ModelRegistry
    try:
        registry = ModelRegistry.load_default()
        cells = select_cells(registry, args.all_catalog, args.cell)
    except Exception as exc:  # noqa: BLE001 - selection errors must produce a failed JSON report
        names = args.cell
        if not names:
            try:
                names = sorted({mode for _, mode in live_defaults()}) or ["candidate"]
            except Exception:  # noqa: BLE001 - even unreadable cell declarations must fail closed
                names = ["candidate"]
        cells = [Cell(name, "unknown", "unknown", None) for name in names]
        registry = None
        selection_error = str(exc)
    else:
        selection_error = None
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix="run-", dir=args.cache_dir))
    runner = Runner(logs)
    if selection_error:
        crash = runner.save([sys.executable, str(Path(__file__))], ROOT, -1, selection_error)
        for cell in cells:
            cell.checks = {kind: outcome("FAIL", "preflight harness could not select cells: " + selection_error, [crash]) for kind in CHECKS}
        report = new_report()
        report.update(cells=[asdict(cell) for cell in cells], commands=[crash.record()])
    else:
        report = run_gate(cells, registry, runner, args)
    print_table(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
